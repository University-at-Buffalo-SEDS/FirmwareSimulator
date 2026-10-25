//! Diagnostic capture must not become an unbounded simulator workload.
use std::{
    collections::VecDeque,
    fs::{self, File},
    io::{self, BufRead, BufWriter, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

const MAX_LINE: usize = 64 * 1024;
const HEAD_BYTES: usize = 64 * 1024;
const TAIL_BYTES: usize = 4 * 1024 * 1024;
const LOG_BYTES: usize = 16 * 1024 * 1024;

/// Consume an entire line while bounding its allocation, including hostile/noisy
/// console output with no newline. Subsequent marker lines remain readable.
pub(crate) fn read_line(reader: &mut impl BufRead) -> io::Result<Option<String>> {
    let mut bytes = Vec::new();
    let mut truncated = false;
    loop {
        let chunk = reader.fill_buf()?;
        if chunk.is_empty() {
            if bytes.is_empty() && !truncated {
                return Ok(None);
            }
            break;
        }
        let end = chunk.iter().position(|b| *b == b'\n');
        let take = end.map_or(chunk.len(), |i| i + 1);
        let keep = take.min(MAX_LINE.saturating_sub(bytes.len()));
        bytes.extend_from_slice(&chunk[..keep]);
        truncated |= keep != take;
        reader.consume(take);
        if end.is_some() {
            break;
        }
    }
    let text = String::from_utf8_lossy(&bytes);
    let mut line = text.trim_end_matches(['\r', '\n']).to_string();
    if truncated {
        line.push_str(" [console line truncated]");
    }
    Ok(Some(line))
}

#[derive(Default)]
pub(crate) struct Capture {
    head: String,
    tail: VecDeque<String>,
    tail_bytes: usize,
    omitted_bytes: u64,
    in_tail: bool,
    markers: Vec<(String, Option<String>)>,
}
impl Capture {
    pub(crate) fn with_markers(markers: Vec<String>) -> Self {
        Self {
            markers: markers.into_iter().map(|marker| (marker, None)).collect(),
            ..Self::default()
        }
    }
    pub(crate) fn push(&mut self, line: &str) {
        // Preserve only actual observations of this run's finite whitelist.
        // Expected-but-unseen markers must never become synthetic successes.
        let trimmed = line.trim();
        for (marker, observed) in &mut self.markers {
            if observed.is_none()
                && (trimmed == marker
                    || (trimmed.ends_with(marker.as_str()) && trimmed.contains("[INFO]")))
            {
                *observed = Some(line.to_owned());
            }
        }
        if !self.in_tail && self.head.len() + line.len() < HEAD_BYTES {
            self.head.push_str(line);
            self.head.push('\n');
            return;
        }
        self.in_tail = true;
        self.tail.push_back(format!("{line}\n"));
        self.tail_bytes += line.len() + 1;
        while self.tail_bytes > TAIL_BYTES {
            let discarded = self.tail.pop_front().expect("nonempty bounded tail");
            self.tail_bytes -= discarded.len();
            self.omitted_bytes += discarded.len() as u64;
        }
    }
    pub(crate) fn finish(self) -> String {
        let mut output = String::new();
        for (_, observation) in self.markers {
            if let Some(line) = observation {
                output.push_str(&line);
                output.push('\n');
            }
        }
        output.push_str(&self.head);
        if self.omitted_bytes != 0 {
            output.push_str(&format!("\n[{} earlier console bytes omitted from in-memory diagnostic; see rotating logs]\n", self.omitted_bytes));
        }
        for line in self.tail {
            output.push_str(&line);
        }
        output
    }
}

/// Two bounded segments retain the most recent output even after a process is
/// killed; the in-memory head independently retains boot output.
pub(crate) struct RotatingLog {
    path: PathBuf,
    writer: BufWriter<File>,
    bytes: usize,
    last_flush: Instant,
    limit: usize,
}
impl RotatingLog {
    pub(crate) fn create(path: PathBuf) -> io::Result<Self> {
        Self::with_limit(path, LOG_BYTES)
    }
    fn with_limit(path: PathBuf, limit: usize) -> io::Result<Self> {
        Ok(Self {
            writer: BufWriter::new(File::create(&path)?),
            path,
            bytes: 0,
            last_flush: Instant::now(),
            limit,
        })
    }
    pub(crate) fn line(&mut self, line: &str, checkpoint: bool) -> io::Result<()> {
        if self.bytes != 0 && self.bytes + line.len() + 1 > self.limit {
            self.writer.flush()?;
            fs::rename(&self.path, self.path.with_extension("previous.log"))?;
            self.writer = BufWriter::new(File::create(&self.path)?);
            self.bytes = 0;
        }
        writeln!(self.writer, "{line}")?;
        self.bytes += line.len() + 1;
        if checkpoint || self.last_flush.elapsed() >= Duration::from_secs(1) {
            self.writer.flush()?;
            self.last_flush = Instant::now();
        }
        Ok(())
    }
    pub(crate) fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()
    }
}

pub(crate) fn live_directory(root: Option<&Path>) -> io::Result<Option<PathBuf>> {
    let Some(root) = root else {
        return Ok(None);
    };
    fs::create_dir_all(root)?;
    let directory = tempfile::Builder::new()
        .prefix("bay-live-")
        .tempdir_in(root)?
        .keep();
    Ok(Some(directory))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn oversized_unterminated_console_line_is_bounded_and_next_marker_survives() {
        let mut source = vec![b'x'; MAX_LINE * 3];
        source.extend_from_slice(b"\nSEDS_BAY_COMPLETE\n");
        let mut reader = io::Cursor::new(source);
        let first = read_line(&mut reader).unwrap().unwrap();
        assert!(first.len() <= MAX_LINE + 30);
        assert!(first.ends_with("[console line truncated]"));
        assert_eq!(
            read_line(&mut reader).unwrap().as_deref(),
            Some("SEDS_BAY_COMPLETE")
        );
        assert!(read_line(&mut reader).unwrap().is_none());
    }
    #[test]
    fn bounded_capture_preserves_boot_and_final_register_dump_under_sustained_noise() {
        let mut capture = Capture::default();
        capture.push("SEDS_BOOT");
        let noise = "x".repeat(4096);
        for _ in 0..10_000 {
            capture.push(&noise);
        }
        capture.push("SEDS_NODE gateway PC\n0x08001234");
        assert!(capture.head.len() <= HEAD_BYTES);
        assert!(capture.tail_bytes <= TAIL_BYTES);
        let output = capture.finish();
        assert!(output.starts_with("SEDS_BOOT\n"));
        assert!(output.ends_with("SEDS_NODE gateway PC\n0x08001234\n"));
        assert!(output.contains("omitted from in-memory diagnostic"));
        assert!(output.len() <= HEAD_BYTES + TAIL_BYTES + 200);
    }
    #[test]
    fn observed_boot_markers_survive_middle_eviction_but_unseen_ones_stay_missing() {
        let mut capture = Capture::with_markers(vec![
            "SEDS_NODE_BOOT_flight".into(),
            "SEDS_NODE_BOOT_missing".into(),
        ]);
        let noise = "x".repeat(4096);
        for _ in 0..32 {
            capture.push(&noise);
        }
        capture.push("[INFO] flight/cpu: SEDS_NODE_BOOT_flight");
        for _ in 0..2048 {
            capture.push(&noise);
        }
        capture.push("SEDS_BAY_COMPLETE");
        let text = capture.finish();
        assert!(text.contains("[INFO] flight/cpu: SEDS_NODE_BOOT_flight\n"));
        assert!(!text.contains("SEDS_NODE_BOOT_missing"));
        assert!(text.ends_with("SEDS_BAY_COMPLETE\n"));
        assert!(text.len() < HEAD_BYTES + TAIL_BYTES + 1024);
    }

    #[test]
    fn live_checkpoint_is_readable_before_completion_and_rotates_with_a_bound() {
        let root = tempfile::tempdir().unwrap();
        let live = live_directory(Some(root.path())).unwrap().unwrap();
        let path = live.join("stdout.log");
        let mut log = RotatingLog::with_limit(path.clone(), 32).unwrap();
        log.line("first checkpoint", true).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "first checkpoint\n");
        log.line("second checkpoint", true).unwrap();
        assert_eq!(
            fs::read_to_string(path.with_extension("previous.log")).unwrap(),
            "first checkpoint\n"
        );
        assert_eq!(fs::read_to_string(&path).unwrap(), "second checkpoint\n");
        log.line("third checkpoint", true).unwrap();
        assert_eq!(
            fs::read_to_string(path.with_extension("previous.log")).unwrap(),
            "second checkpoint\n"
        );
        assert_eq!(fs::read_to_string(&path).unwrap(), "third checkpoint\n");
        assert!(fs::metadata(path).unwrap().len() <= 32);
    }
    #[test]
    fn disabled_live_capture_creates_nothing_and_invalid_directory_fails_explicitly() {
        assert!(live_directory(None).unwrap().is_none());
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("file");
        fs::write(&file, b"x").unwrap();
        assert!(live_directory(Some(&file)).is_err());
    }
}
