//! A small JSON status file for the dashboard, written atomically each tick.

use std::fs;
use std::path::Path;

/// Write via a temp file and rename. Failure to write status must never stop control.
pub fn write_state(path: &Path, payload: &serde_json::Value) {
    let tmp = path.with_extension("json.tmp");
    let written = fs::write(&tmp, payload.to_string()).and_then(|_| fs::rename(&tmp, path));
    if written.is_err() {
        let _ = fs::remove_file(&tmp);
    }
}
