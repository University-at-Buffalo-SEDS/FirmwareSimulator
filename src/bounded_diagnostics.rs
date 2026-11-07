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
const MAX_REBOOT_MARKERS: usize = 64;
const MAX_REBOOT_MARKER_BYTES: usize = 256;

/// Counts reader inputs, never occurrences in rendered head/tail diagnostics.
#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct RebootMarkerObservations {
    pub input_lines: u64,
    pub records: Vec<ObservedRebootMarker>,
    pub unexpected_marker_lines: u64,
    pub count_overflow: bool,
    pub configuration_incomplete: bool,
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct ObservedRebootMarker {
    pub marker: String,
    pub observations: u64,
    pub first_input_line: u64,
    pub last_input_line: u64,
}

#[derive(Default)]
struct RebootMarkers {
    input_lines: u64,
    whitelist: Vec<ObservedRebootMarker>,
    unexpected_marker_lines: u64,
    count_overflow: bool,
    configuration_incomplete: bool,
}

fn increment_bounded(value: &mut u64, overflow: &mut bool) {
    match value.checked_add(1) {
        Some(next) => *value = next,
        None => *overflow = true,
    }
}

fn valid_reboot_marker(marker: &str) -> bool {
    if marker.len() > MAX_REBOOT_MARKER_BYTES {
        return false;
    }
    let mut parts = marker.split(' ');
    let Some("SEDS_BAY_REBOOT") = parts.next() else {
        return false;
    };
    let Some(node) = parts.next() else {
        return false;
    };
    let Some(sample) = parts.next() else {
        return false;
    };
    !node.is_empty()
        && node
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
        && !sample.starts_with('0')
        && sample.bytes().all(|byte| byte.is_ascii_digit())
        && sample.parse::<usize>().is_ok()
        && parts.next().is_none()
}

/// Strip SGR decoration only. Cursor motion, OSC and malformed escape sequences
/// are not marker normalization and cannot turn a console line into evidence.
fn without_sgr(line: &str) -> Option<std::borrow::Cow<'_, str>> {
    let input = line.as_bytes();
    if input.len() > MAX_LINE {
        return None;
    }
    if !input.contains(&0x1b) {
        return Some(std::borrow::Cow::Borrowed(line));
    }
    let mut plain = Vec::with_capacity(input.len());
    let mut index = 0;
    while index < input.len() {
        if input[index] != 0x1b {
            plain.push(input[index]);
            index += 1;
            continue;
        }
        if input.get(index + 1) != Some(&b'[') {
            return None;
        }
        let start = index;
        index += 2;
        while input
            .get(index)
            .is_some_and(|byte| byte.is_ascii_digit() || *byte == b';')
        {
            index += 1;
            if index - start > 32 {
                return None;
            }
        }
        if input.get(index) != Some(&b'm') {
            return None;
        }
        index += 1;
    }
    String::from_utf8(plain).ok().map(std::borrow::Cow::Owned)
}

impl RebootMarkers {
    fn new(markers: Vec<String>) -> Self {
        let mut capture = Self::default();
        for marker in markers {
            if capture.whitelist.len() == MAX_REBOOT_MARKERS
                || !valid_reboot_marker(&marker)
                || capture
                    .whitelist
                    .iter()
                    .any(|record| record.marker == marker)
            {
                // Diagnostic capacity cannot make a previously valid scene
                // unexecutable. Keep finite evidence and report its limits.
                capture.configuration_incomplete = true;
                continue;
            }
            capture.whitelist.push(ObservedRebootMarker {
                marker,
                observations: 0,
                first_input_line: 0,
                last_input_line: 0,
            });
        }
        capture
    }

    fn push(&mut self, line: &str) {
        increment_bounded(&mut self.input_lines, &mut self.count_overflow);
        let Some(plain) = without_sgr(line) else {
            return;
        };
        let marker = plain.trim();
        if let Some(record) = self
            .whitelist
            .iter_mut()
            .find(|record| record.marker == marker)
        {
            if record.observations == 0 {
                record.first_input_line = self.input_lines;
            }
            record.last_input_line = self.input_lines;
            increment_bounded(&mut record.observations, &mut self.count_overflow);
        } else if marker == "SEDS_BAY_REBOOT" || marker.starts_with("SEDS_BAY_REBOOT ") {
            // Preserve a finite refusal count, not arbitrary unbounded payloads.
            increment_bounded(&mut self.unexpected_marker_lines, &mut self.count_overflow);
        }
    }

    fn observations(&self) -> RebootMarkerObservations {
        RebootMarkerObservations {
            input_lines: self.input_lines,
            records: self
                .whitelist
                .iter()
                .filter(|record| record.observations != 0)
                .cloned()
                .collect(),
            unexpected_marker_lines: self.unexpected_marker_lines,
            count_overflow: self.count_overflow,
            configuration_incomplete: self.configuration_incomplete,
        }
    }
}

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
    reboots: RebootMarkers,
}
impl Capture {
    pub(crate) fn with_reboot_markers(
        boot_markers: Vec<String>,
        reboot_markers: Vec<String>,
    ) -> Self {
        Self {
            reboots: RebootMarkers::new(reboot_markers),
            ..Self::with_markers(boot_markers)
        }
    }
    pub(crate) fn with_markers(markers: Vec<String>) -> Self {
        Self {
            markers: markers.into_iter().map(|marker| (marker, None)).collect(),
            ..Self::default()
        }
    }
    pub(crate) fn push(&mut self, line: &str) {
        self.reboots.push(line);
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

    pub(crate) fn finish_with_reboot_observations(self) -> (String, RebootMarkerObservations) {
        let observations = self.reboots.observations();
        (self.finish(), observations)
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
    const REBOOT: &str = "SEDS_BAY_REBOOT gateway 45";

    #[test]
    fn observed_reboot_survives_middle_eviction_without_planned_marker_evidence() {
        let mut capture = Capture::with_reboot_markers(
            Vec::new(),
            vec![REBOOT.into(), "SEDS_BAY_REBOOT actuator 45".into()],
        );
        let noise = "x".repeat(4096);
        for _ in 0..32 {
            capture.push(&noise);
        }
        capture.push(REBOOT);
        for _ in 0..2048 {
            capture.push(&noise);
        }
        let (text, observed) = capture.finish_with_reboot_observations();
        assert!(!text.contains(REBOOT));
        assert!(text.contains("omitted from in-memory diagnostic"));
        assert_eq!(observed.records.len(), 1);
        assert_eq!(observed.records[0].marker, REBOOT);
        assert_eq!(observed.records[0].observations, 1);
        assert_eq!(observed.records[0].first_input_line, 33);
        assert_eq!(observed.records[0].last_input_line, 33);
        assert_eq!(observed.unexpected_marker_lines, 0);
        assert!(!observed.count_overflow);
        assert!(!observed.configuration_incomplete);
        assert!(text.len() <= HEAD_BYTES + TAIL_BYTES + 200);
    }

    #[test]
    fn unseen_reboot_and_script_echo_are_not_observations() {
        let mut capture = Capture::with_reboot_markers(Vec::new(), vec![REBOOT.into()]);
        capture.push("echo \"SEDS_BAY_REBOOT gateway 45\"");
        capture.push("[INFO] monitor: echo \"SEDS_BAY_REBOOT gateway 45\"");
        capture.push("planned: SEDS_BAY_REBOOT gateway 45");
        let (_, observed) = capture.finish_with_reboot_observations();
        assert!(observed.records.is_empty());
        assert_eq!(observed.unexpected_marker_lines, 0);
    }

    #[test]
    fn sgr_decorated_exact_reboot_is_observed_but_cursor_escape_is_not() {
        assert!(matches!(
            without_sgr(REBOOT),
            Some(std::borrow::Cow::Borrowed(_))
        ));
        let mut capture = Capture::with_reboot_markers(Vec::new(), vec![REBOOT.into()]);
        capture.push("\x1b[32mSEDS_BAY_\x1b[0mREBOOT gateway 45\x1b[0m");
        capture.push("\x1b[2KSEDS_BAY_REBOOT gateway 45");
        let (_, observed) = capture.finish_with_reboot_observations();
        assert_eq!(observed.records.len(), 1);
        assert_eq!(observed.records[0].observations, 1);
        assert_eq!(observed.records[0].first_input_line, 1);
        assert_eq!(observed.records[0].last_input_line, 1);
    }

    #[test]
    fn two_actual_reboot_inputs_count_two_while_head_rendering_counts_nothing() {
        let mut capture = Capture::with_reboot_markers(Vec::new(), vec![REBOOT.into()]);
        capture.push(REBOOT);
        capture.push(REBOOT);
        let (text, observed) = capture.finish_with_reboot_observations();
        assert_eq!(text.lines().filter(|line| *line == REBOOT).count(), 2);
        assert_eq!(observed.records[0].observations, 2);
        assert_eq!(observed.records[0].first_input_line, 1);
        assert_eq!(observed.records[0].last_input_line, 2);
    }

    #[test]
    fn retained_boot_head_duplication_does_not_create_extra_reboot_observations() {
        let mut capture = Capture::with_reboot_markers(
            vec!["SEDS_NODE_BOOT_gateway".into()],
            vec![REBOOT.into()],
        );
        capture.push("SEDS_NODE_BOOT_gateway");
        capture.push(REBOOT);
        let (text, observed) = capture.finish_with_reboot_observations();
        assert_eq!(text.matches("SEDS_NODE_BOOT_gateway").count(), 2);
        assert_eq!(observed.records[0].observations, 1);
    }

    #[test]
    fn reboot_whitelist_and_unexpected_input_are_finitely_bounded() {
        let capture = Capture::with_reboot_markers(
            Vec::new(),
            (1..=MAX_REBOOT_MARKERS + 1)
                .map(|sample| format!("SEDS_BAY_REBOOT gateway {sample}"))
                .collect(),
        );
        assert_eq!(capture.reboots.whitelist.len(), MAX_REBOOT_MARKERS);
        assert!(capture.reboots.configuration_incomplete);
        let capture = Capture::with_reboot_markers(Vec::new(), vec![REBOOT.into(), REBOOT.into()]);
        assert!(capture.reboots.configuration_incomplete);
        let capture = Capture::with_reboot_markers(
            Vec::new(),
            vec![format!(
                "SEDS_BAY_REBOOT {} 45",
                "x".repeat(MAX_REBOOT_MARKER_BYTES)
            )],
        );
        assert!(capture.reboots.whitelist.is_empty());
        assert!(capture.reboots.configuration_incomplete);
        let mut capture = Capture::with_reboot_markers(Vec::new(), vec![REBOOT.into()]);
        capture.push("SEDS_BAY_REBOOT unplanned 45");
        capture.push("SEDS_BAY_REBOOT gateway 45 trailing");
        let (_, observed) = capture.finish_with_reboot_observations();
        assert!(observed.records.is_empty());
        assert_eq!(observed.unexpected_marker_lines, 2);
    }

    #[test]
    fn reboot_counter_overflow_is_refusal_evidence_not_wrapped_success() {
        let mut capture = Capture::with_reboot_markers(Vec::new(), vec![REBOOT.into()]);
        capture.reboots.whitelist[0].observations = u64::MAX;
        capture.push(REBOOT);
        let (_, observed) = capture.finish_with_reboot_observations();
        assert_eq!(observed.records[0].observations, u64::MAX);
        assert!(observed.count_overflow);
    }

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
