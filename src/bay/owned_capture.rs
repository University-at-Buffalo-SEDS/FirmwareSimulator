//! Qualification-only export of actual prepared files BEFORE child spawn.
//! No model/clock/IO permission, metadata is never an authority token.
use super::*;
const TREE_LIMIT: u64 = 64 * 1024 * 1024;
const TRACE_LIMIT: usize = 1024 * 1024;

pub(super) fn before_spawn(
    prepared: &PreparedBay,
    manifest: &BuildManifest,
) -> Result<Option<PathBuf>> {
    let Some(base) = env::var_os("OWNED_BAY_TEST_CAPTURE_DIR") else {
        return Ok(None);
    };
    let base = PathBuf::from(base);
    ensure!(
        base.is_absolute() && base.is_dir(),
        "explicit owned capture directory required"
    );
    let destination = base.join(
        prepared
            .scratch
            .path()
            .file_name()
            .context("scratch name")?,
    );
    // No overwrite of a prior instance's artifacts, including failed runs.
    fs::create_dir(&destination)?;
    let actual = prepared.scratch.path();
    let mut pending = vec![actual.to_owned()];
    let mut files = Vec::new();
    let mut bytes = 0u64;
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            ensure!(!kind.is_symlink(), "generated capture symlink refused");
            if kind.is_dir() {
                pending.push(entry.path());
            } else {
                ensure!(kind.is_file(), "non-file generated capture refused");
                bytes = bytes
                    .checked_add(entry.metadata()?.len())
                    .context("capture size overflow")?;
                ensure!(
                    bytes <= TREE_LIMIT && files.len() < 4096,
                    "prepared tree exceeds64MiB/4096files"
                );
                files.push(entry.path());
            }
        }
    }
    let mut rows = Vec::new();
    for file in files {
        let relative = file.strip_prefix(actual)?;
        let output = destination.join(relative);
        fs::create_dir_all(output.parent().context("capture parent")?)?;
        fs::copy(&file, &output)?;
        let original_hash = digest(&file)?;
        ensure!(
            original_hash == digest(&output)?,
            "prepared capture not byteexact"
        );
        rows.push(serde_json::json!({"relative":relative,"actual_source":file,
            "captured":output,"sha256":original_hash}));
    }
    let facts = serde_json::json!({"scope":"ACTUAL GENERATED FILES copied before Renode spawn; not a reconstructed script",
        "actual_namespace":actual,"captured_bytes":bytes,"tree_limit_bytes":TREE_LIMIT,
        "files":rows,"catalog":manifest.files.iter().map(|file|serde_json::json!({
            "role":file.relative_role,"sha256":file.sha256})).collect::<Vec<_>>(),
        "mountinfo":fs::read_to_string("/proc/self/mountinfo")?});
    fs::write(
        destination.join("ACTUAL-PREPARED-INPUTS.json"),
        serde_json::to_vec_pretty(&facts)?,
    )?;
    println!(
        "ACTUAL_OWNED_BAY_PREPARED_CAPTURE {}",
        serde_json::json!({
        "directory":destination,"files":rows.len(),"bytes":bytes,"before_spawn":true})
    );
    Ok(Some(destination))
}

pub(super) struct Trace {
    file: Option<fs::File>,
    remaining: usize,
}
impl Trace {
    pub(super) fn open(directory: Option<&Path>, name: &str) -> Result<Self> {
        let file = directory
            .map(|path| {
                OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(path.join(format!("actual-renode-{name}.log")))
            })
            .transpose()?;
        Ok(Self {
            file,
            remaining: TRACE_LIMIT,
        })
    }
    pub(super) fn line(&mut self, line: &str) -> Result<()> {
        if let Some(file) = &mut self.file {
            let bytes = line.as_bytes();
            let count = bytes.len().min(self.remaining);
            file.write_all(&bytes[..count])?;
            self.remaining -= count;
            if self.remaining > 0 {
                file.write_all(b"\n")?;
                self.remaining -= 1;
            }
        }
        Ok(())
    }
}
