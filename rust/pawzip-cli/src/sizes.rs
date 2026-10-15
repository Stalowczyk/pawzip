//! Parsing option values (sizes such as `64K`, modes, worker counts) and printing numbers.

use pawzip::{Mode, FORMAT_MAX_BLOCK_SIZE};

/// Peak memory per block byte, for `--workers auto` (see the Python `paw.parallel`).
pub const COMPRESS_MEMORY_PER_BYTE: u64 = 100;
pub const DECODE_MEMORY_PER_BYTE: u64 = 8;

/// A byte count such as `5000`, `64K`, `100M`, `1.5G` or `2GB`.
pub fn parse_size(text: &str) -> Result<u64, String> {
    let upper = text.trim().to_uppercase();
    let body = upper.strip_suffix('B').unwrap_or(&upper);
    let (number, unit) = match body.chars().last() {
        Some('K') => (&body[..body.len() - 1], 1024f64),
        Some('M') => (&body[..body.len() - 1], 1024f64 * 1024.0),
        Some('G') => (&body[..body.len() - 1], 1024f64 * 1024.0 * 1024.0),
        _ => (body, 1f64),
    };
    let value = number
        .trim()
        .parse::<f64>()
        .ok()
        .map(|v| (v * unit).trunc())
        .filter(|v| v.is_finite() && *v < 1.8e19)
        .ok_or_else(|| format!("invalid size: {body:?}"))?;
    if value < 1.0 {
        return Err("size must be at least 1 byte".into());
    }
    Ok(value as u64)
}

pub fn parse_block_size(text: &str) -> Result<u64, String> {
    let value = parse_size(text)?;
    if value > FORMAT_MAX_BLOCK_SIZE {
        return Err("block size must be below 4G".into());
    }
    Ok(value)
}

pub fn parse_mode(label: &str) -> Result<Mode, String> {
    let modes: Vec<Mode> = (0..=u8::MAX).map_while(Mode::from_byte).collect();
    modes
        .iter()
        .copied()
        .find(|m| label == m.label() || label == (*m as u8).to_string())
        .ok_or_else(|| {
            let choices: Vec<&str> = modes.iter().map(|m| m.label()).collect();
            format!(
                "unknown mode {label:?} (choose from {})",
                choices.join(", ")
            )
        })
}

#[derive(Clone, Copy)]
pub enum Workers {
    Count(usize),
    Auto,
}

pub fn parse_workers(text: &str) -> Result<Workers, String> {
    if text == "auto" {
        return Ok(Workers::Auto);
    }
    match text.parse::<usize>() {
        Ok(n) if n >= 1 && text.chars().all(|c| c.is_ascii_digit()) => Ok(Workers::Count(n)),
        _ => Err("workers must be a positive number or 'auto'".into()),
    }
}

impl Workers {
    /// The number of workers: "auto" uses one per CPU core, but only as many as fit into
    /// half of the memory available right now, and one if that can't be measured.
    pub fn count(self, block_size: u64, per_byte: u64) -> usize {
        match self {
            Workers::Count(n) => n,
            Workers::Auto => {
                let cpus = std::thread::available_parallelism().map_or(1, |n| n.get());
                match available_memory() {
                    None => 1,
                    Some(free) => {
                        let per_worker = per_byte.saturating_mul(block_size).max(1);
                        cpus.min((free / 2 / per_worker) as usize).max(1)
                    }
                }
            }
        }
    }
}

/// Bytes of physical memory currently available, if known.
#[cfg(windows)]
fn available_memory() -> Option<u64> {
    #[repr(C)]
    struct MemoryStatusEx {
        length: u32,
        memory_load: u32,
        total_phys: u64,
        avail_phys: u64,
        total_page_file: u64,
        avail_page_file: u64,
        total_virtual: u64,
        avail_virtual: u64,
        avail_extended_virtual: u64,
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GlobalMemoryStatusEx(status: *mut MemoryStatusEx) -> i32;
    }
    let mut status = MemoryStatusEx {
        length: std::mem::size_of::<MemoryStatusEx>() as u32,
        memory_load: 0,
        total_phys: 0,
        avail_phys: 0,
        total_page_file: 0,
        avail_page_file: 0,
        total_virtual: 0,
        avail_virtual: 0,
        avail_extended_virtual: 0,
    };
    // SAFETY: a correctly sized struct with `length` set, as the API requires.
    let ok = unsafe { GlobalMemoryStatusEx(&mut status) };
    (ok != 0).then_some(status.avail_phys)
}

#[cfg(not(windows))]
fn available_memory() -> Option<u64> {
    let text = std::fs::read_to_string("/proc/meminfo").ok()?;
    let line = text.lines().find(|l| l.starts_with("MemAvailable:"))?;
    let kib: u64 = line.split_whitespace().nth(1)?.parse().ok()?;
    Some(kib * 1024)
}

/// 1234567 -> "1,234,567", as the Python package prints numbers.
pub fn group_thousands(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes_parse_like_the_python_cli() {
        assert_eq!(parse_size("5000"), Ok(5000));
        assert_eq!(parse_size("64K"), Ok(65536));
        assert_eq!(parse_size("1.5m"), Ok(1572864));
        assert_eq!(parse_size("2GB"), Ok(2 << 30));
        for bad in ["0", "lots", "inf", "1e400", "", "-5"] {
            assert!(parse_size(bad).is_err(), "{bad}");
        }
        assert!(parse_block_size("5G").is_err());
        assert_eq!(parse_mode("bwt-mtf-map"), Ok(Mode::BwtMtfMap));
        assert_eq!(parse_mode("3"), Ok(Mode::DeltaMap));
        assert_eq!(parse_mode("bwt-map"), Ok(Mode::BwtMap));
        assert_eq!(parse_mode("6"), Ok(Mode::DeltaBwtMap));
        assert_eq!(parse_mode("x86-bwt-map"), Ok(Mode::X86BwtMap));
        assert!(parse_mode("8").is_err());
        assert!(parse_mode("zip").is_err());
        assert!(parse_workers("0").is_err() && parse_workers("x").is_err());
        assert!(matches!(parse_workers("auto"), Ok(Workers::Auto)));
        assert_eq!(group_thousands(0), "0");
        assert_eq!(group_thousands(1234567), "1,234,567");
        assert_eq!(group_thousands(100), "100");
    }
}
