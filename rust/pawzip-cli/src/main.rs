//! `paw compress | decompress | test | info`: the command line of pawzip, mirroring the
//! Python package's `paw` command (same options, messages, file names and exit codes).
//!
//! Everything streams block by block, so files of any size work with bounded memory.
//! Status messages go to stderr so that data written to stdout (`-c` or `-` input) is never
//! mixed with them. Exit status: 0 on success, 1 if any file failed, 2 on usage errors,
//! 130 if interrupted (Ctrl-C).

mod names;
mod progress;
mod sizes;

use std::fs::File;
use std::io::{self, IsTerminal, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Instant;

use clap::{CommandFactory, Parser, Subcommand};
use pawzip::{CompressOptions, DecompressOptions, Mode, DEFAULT_MAX_BLOCK_SIZE};

use names::{compressed_name, decompressed_name, SUFFIX};
use progress::ProgressLine;
use sizes::{group_thousands, parse_block_size, parse_mode, parse_size, parse_workers, Workers};

const STDIO: &str = "-";

#[derive(Parser)]
#[command(
    name = "pawzip",
    bin_name = "paw",
    version,
    about = "Compress and decompress .paw files."
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(clap::Args)]
struct Output {
    /// Output file (only with a single input)
    #[arg(short, long)]
    output: Option<PathBuf>,
    /// Write to stdout
    #[arg(short = 'c', long = "stdout")]
    stdout: bool,
    /// Overwrite existing files
    #[arg(short, long)]
    force: bool,
    /// No status messages
    #[arg(short, long)]
    quiet: bool,
}

#[derive(clap::Args)]
struct Limits {
    /// Refuse outputs larger than this (e.g. 500M)
    #[arg(long, value_parser = parse_size)]
    max_size: Option<u64>,
    /// Refuse blocks larger than this (default 64M; decoding needs about 7x a block)
    #[arg(long, value_parser = parse_size, default_value_t = DEFAULT_MAX_BLOCK_SIZE)]
    max_block_size: u64,
}

#[derive(Subcommand)]
enum Command {
    /// Compress FILE to FILE.paw.
    Compress {
        /// Input files (- for stdin)
        #[arg(required = true, value_name = "FILE")]
        files: Vec<String>,
        #[command(flatten)]
        out: Output,
        /// Blocks coded in parallel, or 'auto' to fit the CPU and free memory (default 1)
        #[arg(short = 'T', long, value_parser = parse_workers, default_value = "1")]
        workers: Workers,
        /// Try only this mode (repeatable): stored, map, bwt-mtf-map, delta-map,
        /// delta-bwt-mtf-map, bwt-map, delta-bwt-map, x86-bwt-map
        #[arg(short, long = "mode", value_parser = parse_mode)]
        modes: Vec<Mode>,
        /// Block size, e.g. 4M (default 16M); compressing needs about 100x this per worker,
        /// and blocks over 64M need --max-block-size to decompress
        #[arg(short = 'B', long, value_parser = parse_block_size, default_value = "16M")]
        block_size: u64,
        /// Try every mode even on already-compressed data (JPEG, ZIP...): much slower, saves a little
        #[arg(long)]
        thorough: bool,
    },
    /// Decompress FILE.paw to FILE.
    Decompress {
        /// Input files (- for stdin)
        #[arg(required = true, value_name = "FILE")]
        files: Vec<String>,
        #[command(flatten)]
        out: Output,
        /// Blocks coded in parallel, or 'auto' to fit the CPU and free memory (default 1)
        #[arg(short = 'T', long, value_parser = parse_workers, default_value = "1")]
        workers: Workers,
        #[command(flatten)]
        limits: Limits,
    },
    /// Check that .paw files decompress correctly.
    Test {
        /// Input files (- for stdin)
        #[arg(required = true, value_name = "FILE")]
        files: Vec<String>,
        /// Blocks coded in parallel, or 'auto' to fit the CPU and free memory (default 1)
        #[arg(short = 'T', long, value_parser = parse_workers, default_value = "1")]
        workers: Workers,
        #[command(flatten)]
        limits: Limits,
    },
    /// Show the headers of .paw files.
    Info {
        /// Input files (- for stdin)
        #[arg(required = true, value_name = "FILE")]
        files: Vec<String>,
    },
}

/// The temporary output being written, removed if the program is interrupted.
static PARTIAL: Mutex<Option<PathBuf>> = Mutex::new(None);

fn main() {
    let cli = Cli::parse();
    check_usage(&cli);
    let _ = ctrlc::set_handler(|| {
        if let Some(tmp) = PARTIAL.lock().ok().and_then(|mut p| p.take()) {
            let _ = std::fs::remove_file(tmp);
        }
        eprintln!("paw: interrupted");
        std::process::exit(130);
    });
    let (files, is_test) = match &cli.command {
        Command::Compress { files, .. }
        | Command::Decompress { files, .. }
        | Command::Info { files } => (files, false),
        Command::Test { files, .. } => (files, true),
    };
    let mut status = 0;
    for name in files {
        if let Err(message) = run(&cli.command, name) {
            eprintln!("paw: {name}: {message}");
            if is_test {
                println!("{name}: FAILED");
            }
            status = 1;
        }
    }
    std::process::exit(status);
}

/// Usage errors clap can't express (exit status 2, like any usage error).
fn check_usage(cli: &Cli) {
    let usage = |message: &str| -> ! {
        Cli::command()
            .error(clap::error::ErrorKind::ArgumentConflict, message)
            .exit()
    };
    match &cli.command {
        Command::Compress { files, out, .. } | Command::Decompress { files, out, .. } => {
            if out.output.is_some() && files.len() > 1 {
                usage("-o/--output needs exactly one input file");
            }
            if matches!(cli.command, Command::Compress { .. }) && out.output.is_none() {
                let to_stdout = if out.stdout {
                    files.len()
                } else {
                    files.iter().filter(|f| *f == STDIO).count()
                };
                if to_stdout > 1 {
                    // several streams back to back would not be a valid .paw file
                    usage("compress can write only one input to stdout");
                }
            }
        }
        _ => {}
    }
}

fn run(command: &Command, name: &str) -> Result<(), String> {
    match command {
        Command::Compress {
            out,
            workers,
            modes,
            block_size,
            thorough,
            ..
        } => {
            let start = Instant::now();
            let (default, extension) = if name == STDIO {
                (None, None)
            } else {
                compressed_name(Path::new(name))
            };
            let mut options = CompressOptions::default();
            options.block_size = *block_size;
            options.modes = (!modes.is_empty()).then(|| modes.clone());
            options.workers = workers.count(*block_size, sizes::COMPRESS_MEMORY_PER_BYTE);
            options.extension = extension;
            options.thorough = *thorough;
            let (place, original, written) = convert(out, name, default, |src, dst, progress| {
                pawzip::compress_stream(src, dst, &options, progress)
            })?;
            let ratio = if original > 0 {
                format!("{:.1}%", 100.0 * written as f64 / original as f64)
            } else {
                "-".into()
            };
            say(
                out,
                &format!(
                    "{name} -> {place}: {} -> {} bytes ({ratio}), {:.1}s",
                    group_thousands(original),
                    group_thousands(written),
                    start.elapsed().as_secs_f64()
                ),
            );
        }
        Command::Decompress {
            out,
            workers,
            limits,
            ..
        } => {
            let mut default = None;
            if name != STDIO && out.output.is_none() && !out.stdout {
                // the output name depends on the stored extension
                let header = pawzip::read_header(open(name)?).map_err(|e| e.to_string())?;
                default = decompressed_name(Path::new(name), header.extension.as_deref());
            }
            let options = decode_options(limits, workers);
            let (place, original, _) = convert(out, name, default, |src, dst, progress| {
                pawzip::decompress_stream(src, dst, &options, progress)
            })?;
            say(
                out,
                &format!("{name} -> {place}: {} bytes", group_thousands(original)),
            );
        }
        Command::Test {
            workers, limits, ..
        } => {
            let src = input(name)?;
            let total = input_size(name);
            let mut line = ProgressLine::start(name, total, show_progress(false, false));
            let result = pawzip::decompress_stream(
                src,
                io::sink(),
                &decode_options(limits, workers),
                &mut |done| line.update(done),
            );
            line.finish();
            let size = result.map_err(|e| e.to_string())?;
            println!("{name}: OK ({} bytes)", group_thousands(size));
        }
        Command::Info { .. } => {
            let meta = pawzip::info_stream(input(name)?).map_err(|e| e.to_string())?;
            let ratio = if meta.original_size > 0 {
                meta.compressed_size as f64 / meta.original_size as f64
            } else {
                1.0
            };
            let extension = meta
                .extension
                .as_ref()
                .map_or("-".to_string(), |e| format!(".{e}"));
            println!(
                "{name}\n  original:    {} bytes\n  compressed:  {} bytes ({:.1}%)\n  \
                 blocks:      {} (block size {} bytes)\n  modes:       {}\n  \
                 extension:   {extension}\n  format:      v{}",
                group_thousands(meta.original_size),
                group_thousands(meta.compressed_size),
                100.0 * ratio,
                group_thousands(meta.modes.len() as u64),
                group_thousands(meta.block_size),
                mode_summary(&meta.modes),
                pawzip::FORMAT_VERSION
            );
        }
    }
    Ok(())
}

fn decode_options(limits: &Limits, workers: &Workers) -> DecompressOptions {
    let mut options = DecompressOptions::default();
    options.max_size = limits.max_size;
    options.max_block_size = Some(limits.max_block_size);
    options.workers = workers.count(limits.max_block_size, sizes::DECODE_MEMORY_PER_BYTE);
    options
}

/// The block modes as text: "bwt-mtf-map", or "bwt-mtf-map x3, map x1" (most common first).
fn mode_summary(modes: &[Mode]) -> String {
    let mut counts: Vec<(Mode, usize)> = Vec::new();
    for &m in modes {
        match counts.iter_mut().find(|(x, _)| *x == m) {
            Some((_, c)) => *c += 1,
            None => counts.push((m, 1)),
        }
    }
    if counts.len() == 1 && modes.len() == 1 {
        return modes[0].label().to_string();
    }
    counts.sort_by_key(|&(_, c)| std::cmp::Reverse(c)); // stable: ties keep their order
    let parts: Vec<String> = counts
        .iter()
        .map(|(m, c)| format!("{} x{c}", m.label()))
        .collect();
    if parts.is_empty() {
        "-".into()
    } else {
        parts.join(", ")
    }
}

fn say(out: &Output, message: &str) {
    if !out.quiet {
        eprintln!("{message}");
    }
}

fn open(name: &str) -> Result<File, String> {
    File::open(name).map_err(|e| e.to_string())
}

fn input(name: &str) -> Result<Box<dyn Read>, String> {
    if name == STDIO {
        Ok(Box::new(io::stdin().lock()))
    } else {
        Ok(Box::new(open(name)?))
    }
}

/// Size of the regular file being read, or None (stdin, pipes).
fn input_size(name: &str) -> Option<u64> {
    if name == STDIO {
        return None;
    }
    std::fs::metadata(name)
        .ok()
        .filter(|m| m.is_file())
        .map(|m| m.len())
}

/// Progress goes to stderr, and only when someone is watching: never with --quiet, when
/// stderr is redirected, or when the data itself is being written to the terminal.
fn show_progress(quiet: bool, to_stdout: bool) -> bool {
    !quiet && io::stderr().is_terminal() && !(to_stdout && io::stdout().is_terminal())
}

/// Counts the bytes written through it.
struct Counter<W: Write> {
    inner: W,
    count: u64,
}

impl<W: Write> Write for Counter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.count += n as u64;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

/// Stream `name` through `convert` to stdout or a file, showing progress. Returns the
/// destination, the count `convert` returns, and the bytes written.
fn convert(
    out: &Output,
    name: &str,
    default: Option<PathBuf>,
    mut convert: impl FnMut(Box<dyn Read>, &mut dyn Write, &mut dyn FnMut(u64)) -> pawzip::Result<u64>,
) -> Result<(String, u64, u64), String> {
    let to_stdout = out.stdout || (name == STDIO && out.output.is_none());
    let src = input(name)?;
    let mut line = ProgressLine::start(name, input_size(name), show_progress(out.quiet, to_stdout));
    let result = if to_stdout {
        let mut dst = Counter {
            inner: io::stdout().lock(),
            count: 0,
        };
        convert(src, &mut dst, &mut |done| line.update(done))
            .map(|processed| ("stdout".to_string(), processed, dst.count))
            .map_err(|e| e.to_string())
    } else {
        match out.output.clone().or(default) {
            None => Err(format!("has no {SUFFIX} suffix; use -o to name the output")),
            Some(dst) => write_atomically(&dst, out.force, |f| {
                let mut counter = Counter { inner: f, count: 0 };
                let processed = convert(src, &mut counter, &mut |done| line.update(done))
                    .map_err(|e| e.to_string())?;
                Ok((processed, counter.count))
            })
            .map(|(processed, written)| (dst.display().to_string(), processed, written)),
        }
    };
    line.finish();
    result
}

/// Let `produce` write into a temporary file next to `path`, then move it into place, so a
/// failure (or Ctrl-C) never leaves a partial output file behind.
fn write_atomically<T>(
    path: &Path,
    overwrite: bool,
    produce: impl FnOnce(&mut io::BufWriter<File>) -> Result<T, String>,
) -> Result<T, String> {
    if path.exists() && !overwrite {
        return Err(format!(
            "{} already exists (use -f to overwrite)",
            path.display()
        ));
    }
    let (file, tmp) = names::create_temp(path).map_err(|e| e.to_string())?;
    *PARTIAL.lock().unwrap() = Some(tmp.clone());
    let mut writer = io::BufWriter::with_capacity(1 << 16, file);
    let result = produce(&mut writer).and_then(|value| {
        writer.flush().map_err(|e| e.to_string())?;
        drop(writer);
        std::fs::rename(&tmp, path).map_err(|e| e.to_string())?;
        Ok(value)
    });
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    *PARTIAL.lock().unwrap() = None;
    result
}
