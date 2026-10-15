//! File names, as in the Python package's `paw.files`: `photo.png` <-> `photo.paw`, with the
//! extension stored inside, so a renamed `abc.paw` decompresses to `abc.png`.

use std::fs::{File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub const SUFFIX: &str = ".paw";

/// The suffix of a file name as Python's `pathlib` sees it: from the last dot, if that dot
/// is neither the first nor the last character (`.bashrc` and `notes.` have none).
fn suffix(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[i..],
        _ => "",
    }
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// The extension to store for `suffix` (".png" -> "png"), if it can be stored.
fn storable(suffix: &str) -> Option<String> {
    let ext = suffix.strip_prefix('.').unwrap_or(suffix);
    let ok = !ext.is_empty() && pawzip::extension_allowed(ext);
    ok.then(|| ext.to_string())
}

/// The default .paw name for `src` and the extension to store in it: `photo.png` ->
/// (`photo.paw`, "png"). Files without a storable extension, and .paw files themselves,
/// keep their whole name: `notes` -> `notes.paw`.
pub fn compressed_name(src: &Path) -> (Option<PathBuf>, Option<String>) {
    let name = file_name(src);
    let sfx = suffix(&name);
    if !sfx.is_empty() && !sfx.eq_ignore_ascii_case(SUFFIX) {
        if let Some(ext) = storable(sfx) {
            let stem = &name[..name.len() - sfx.len()];
            return (
                Some(src.with_file_name(format!("{stem}{SUFFIX}"))),
                Some(ext),
            );
        }
    }
    (Some(src.with_file_name(format!("{name}{SUFFIX}"))), None)
}

/// The default output name for the .paw file `src` holding `extension`: `abc.paw` holding
/// "png" -> `abc.png`, whatever the .paw file was renamed to; `photo.png.paw` (gzip-style naming)
/// -> `photo.png`. None if there is nothing to go on.
pub fn decompressed_name(src: &Path, extension: Option<&str>) -> Option<PathBuf> {
    let name = file_name(src);
    let sfx = suffix(&name);
    let mut base = if sfx.eq_ignore_ascii_case(SUFFIX) {
        name[..name.len() - sfx.len()].to_string()
    } else if extension.is_some() {
        name.clone()
    } else {
        return None;
    };
    if let Some(ext) = extension {
        let tail = format!(".{}", ext.to_lowercase());
        if !base.to_lowercase().ends_with(&tail) {
            base = format!("{base}.{ext}");
        }
    }
    Some(src.with_file_name(base))
}

/// Create a new, empty file next to `path` (with the permissions a plain create gives).
pub fn create_temp(path: &Path) -> io::Result<(File, PathBuf)> {
    let name = file_name(path);
    let seed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos() as u64)
        ^ u64::from(std::process::id()) << 32;
    for attempt in 0..100u64 {
        let tag = seed.wrapping_add(attempt.wrapping_mul(0x9E37_79B9_7F4A_7C15)) & 0xFFFF_FFFF_FFFF;
        let tmp = path.with_file_name(format!(".{name}.{tag:012x}.tmp"));
        match OpenOptions::new().write(true).create_new(true).open(&tmp) {
            Ok(f) => return Ok((f, tmp)),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        format!(
            "could not create a temporary file next to {}",
            path.display()
        ),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c(name: &str) -> (String, Option<String>) {
        let (p, e) = compressed_name(Path::new(name));
        (p.unwrap().display().to_string(), e)
    }

    fn d(name: &str, ext: Option<&str>) -> Option<String> {
        decompressed_name(Path::new(name), ext).map(|p| p.display().to_string())
    }

    #[test]
    fn names_match_the_python_package() {
        assert_eq!(c("photo.png"), ("photo.paw".into(), Some("png".into())));
        assert_eq!(c("notes"), ("notes.paw".into(), None));
        assert_eq!(c("x.paw"), ("x.paw.paw".into(), None));
        assert_eq!(c(".bashrc"), (".bashrc.paw".into(), None));
        assert_eq!(c("a.tar.gz"), ("a.tar.paw".into(), Some("gz".into())));
        assert_eq!(c("weird.ex t"), ("weird.ex t.paw".into(), None));
        assert_eq!(d("abc.paw", Some("png")).as_deref(), Some("abc.png"));
        assert_eq!(
            d("photo.png.paw", Some("png")).as_deref(),
            Some("photo.png")
        );
        assert_eq!(d("ABC.PAW", None).as_deref(), Some("ABC"));
        assert_eq!(d("renamed", Some("txt")).as_deref(), Some("renamed.txt"));
        assert_eq!(d("renamed", None), None);
    }
}
