//! The `paw` binary end to end (rust/cli_parity.py also checks it against the Python CLI).

use std::path::PathBuf;
use std::process::{Command, Output};

fn paw(dir: &PathBuf, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_paw"))
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap()
}

fn temp_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("paw-cli-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn round_trip_with_names_from_the_stored_extension() {
    let dir = temp_dir("roundtrip");
    let data: Vec<u8> = b"the map, the series and the gaps.\n".repeat(3000);
    std::fs::write(dir.join("notes.txt"), &data).unwrap();
    let out = paw(&dir, &["compress", "-B", "8K", "-T", "2", "notes.txt"]);
    assert!(out.status.success(), "{out:?}");
    std::fs::rename(dir.join("notes.paw"), dir.join("renamed.paw")).unwrap();
    std::fs::remove_file(dir.join("notes.txt")).unwrap();
    assert!(paw(&dir, &["test", "-T", "auto", "renamed.paw"])
        .status
        .success());
    let out = paw(&dir, &["decompress", "renamed.paw"]);
    assert!(out.status.success(), "{out:?}");
    assert_eq!(std::fs::read(dir.join("renamed.txt")).unwrap(), data);
    // the output is identical to the library's
    let options = pawzip_core::Options {
        block_size: 8192,
        extension: Some("txt".into()),
        ..Default::default()
    };
    assert_eq!(
        std::fs::read(dir.join("renamed.paw")).unwrap(),
        pawzip_core::compress(&data, &options).unwrap()
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn failures_leave_no_partial_files_and_set_the_exit_status() {
    let dir = temp_dir("failures");
    std::fs::write(dir.join("fake.paw"), b"PAW\x01 not really").unwrap();
    let out = paw(&dir, &["decompress", "-o", "out.bin", "fake.paw"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).starts_with("paw: fake.paw: "));
    let left: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    assert_eq!(left, ["fake.paw"]); // no out.bin, no temporary file
    assert_eq!(
        paw(&dir, &["compress", "-B", "0", "fake.paw"])
            .status
            .code(),
        Some(2)
    );
    let _ = std::fs::remove_dir_all(&dir);
}
