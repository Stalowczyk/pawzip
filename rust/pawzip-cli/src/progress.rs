//! One line of progress on stderr, redrawn after every block and once a second in between,
//! and erased at the end (as in the Python CLI).

use std::io::Write;
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// A byte count for people: 900 B, 3.4 KiB, 120.0 MiB.
fn amount(n: u64) -> String {
    if n < 1024 {
        return format!("{n} B");
    }
    let mut value = n as f64 / 1024.0;
    let mut unit = "KiB";
    for bigger in ["MiB", "GiB", "TiB"] {
        if value < 1024.0 {
            break;
        }
        value /= 1024.0;
        unit = bigger;
    }
    format!("{value:.1} {unit}")
}

/// 0:07, 12:34 or 1:02:03.
fn clock(seconds: f64) -> String {
    let s = seconds as u64;
    let (hours, minutes, s) = (s / 3600, s / 60 % 60, s % 60);
    if hours > 0 {
        format!("{hours}:{minutes:02}:{s:02}")
    } else {
        format!("{minutes}:{s:02}")
    }
}

fn text(name: &str, done: u64, total: Option<u64>, elapsed: f64) -> String {
    match total {
        Some(total) if total > 0 => {
            let percent = 100 * done.min(total) / total;
            let mut line = format!(
                "{name}: {percent:3}% of {}, {} elapsed",
                amount(total),
                clock(elapsed)
            );
            if done > 0 && done < total {
                let left = elapsed * (total - done) as f64 / done as f64;
                line += &format!(", about {} left", clock(left));
            }
            line
        }
        // size unknown (a pipe), or an empty file
        _ => format!(
            "{name}: {} so far, {} elapsed",
            amount(done),
            clock(elapsed)
        ),
    }
}

fn columns() -> usize {
    std::env::var("COLUMNS")
        .ok()
        .and_then(|c| c.parse().ok())
        .unwrap_or(80)
}

struct State {
    name: String,
    total: Option<u64>,
    done: u64,
    start: Instant,
    shown: usize, // length of the text currently on screen
    stopped: bool,
}

impl State {
    fn draw(&mut self) {
        let width = columns().saturating_sub(1);
        let elapsed = self.start.elapsed().as_secs_f64();
        let mut line = text(&self.name, self.done, self.total, elapsed);
        let chars = self.name.chars().count();
        if line.chars().count() > width && chars > 8 {
            // shorten the name, keeping its end
            let over = line.chars().count() - width;
            let keep = chars.saturating_sub(over + 3).max(8);
            let tail: String = self.name.chars().skip(chars - keep.min(chars)).collect();
            line = text(&format!("...{tail}"), self.done, self.total, elapsed);
        }
        let line: String = line.chars().take(width).collect();
        let len = line.chars().count();
        let mut err = std::io::stderr().lock();
        let _ = write!(err, "\r{line:<width$}", width = self.shown.max(len));
        let _ = err.flush();
        self.shown = len;
    }
}

/// Shown only when `enabled`; otherwise every method does nothing.
pub struct ProgressLine {
    state: Option<Arc<(Mutex<State>, Condvar)>>,
    ticker: Option<JoinHandle<()>>,
}

impl ProgressLine {
    pub fn start(name: &str, total: Option<u64>, enabled: bool) -> Self {
        if !enabled {
            return ProgressLine {
                state: None,
                ticker: None,
            };
        }
        let state = Arc::new((
            Mutex::new(State {
                name: if name == "-" {
                    "stdin".into()
                } else {
                    name.into()
                },
                total,
                done: 0,
                start: Instant::now(),
                shown: 0,
                stopped: false,
            }),
            Condvar::new(),
        ));
        state.0.lock().unwrap().draw();
        let shared = Arc::clone(&state);
        let ticker = std::thread::spawn(move || {
            let (lock, wake) = &*shared;
            let mut s = lock.lock().unwrap();
            while !s.stopped {
                s = wake.wait_timeout(s, Duration::from_secs(1)).unwrap().0;
                if !s.stopped {
                    s.draw();
                }
            }
        });
        ProgressLine {
            state: Some(state),
            ticker: Some(ticker),
        }
    }

    pub fn update(&mut self, done: u64) {
        if let Some(state) = &self.state {
            let mut s = state.0.lock().unwrap();
            s.done = done;
            s.draw();
        }
    }

    /// Stop redrawing and erase the line.
    pub fn finish(&mut self) {
        if let Some(state) = self.state.take() {
            {
                let mut s = state.0.lock().unwrap();
                s.stopped = true;
                state.1.notify_all();
                let mut err = std::io::stderr().lock();
                let _ = write!(err, "\r{}\r", " ".repeat(s.shown));
                let _ = err.flush();
            }
            if let Some(t) = self.ticker.take() {
                let _ = t.join();
            }
        }
    }
}

impl Drop for ProgressLine {
    fn drop(&mut self) {
        self.finish();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn texts_match_the_python_cli() {
        assert_eq!(amount(900), "900 B");
        assert_eq!(amount(3482), "3.4 KiB");
        assert_eq!(amount(120 << 20), "120.0 MiB");
        assert_eq!(clock(7.9), "0:07");
        assert_eq!(clock(3723.0), "1:02:03");
        assert_eq!(
            text("a.txt", 50, Some(100), 10.0),
            "a.txt:  50% of 100 B, 0:10 elapsed, about 0:10 left"
        );
        assert_eq!(
            text("stdin", 2048, None, 3.0),
            "stdin: 2.0 KiB so far, 0:03 elapsed"
        );
    }
}
