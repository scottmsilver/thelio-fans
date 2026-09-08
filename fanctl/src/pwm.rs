//! The one file this program writes: the intake channel's PWM attribute on the Io board.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// The hwmon `name` of the Thelio Io board's driver.
pub const DEVICE_NAME: &str = "system76_io";

/// The hwmon directory whose `name` attribute matches, or None. Scanned fresh each call
/// because the Io board renumbers when it re-enumerates on USB.
pub fn find_device(root: &Path, name: &str) -> Option<PathBuf> {
    let mut dirs: Vec<PathBuf> = fs::read_dir(root.join("class/hwmon"))
        .ok()?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with("hwmon"))
        })
        .collect();
    dirs.sort();
    dirs.into_iter()
        .find(|d| fs::read_to_string(d.join("name")).is_ok_and(|s| s.trim() == name))
}

/// Handle on the intake channel's `pwm2` attribute. The path is re-resolved whenever it
/// stops existing or a read fails, because the board renumbers on USB re-enumeration.
pub struct Pwm {
    root: PathBuf,
    channel: String,
    path: Option<PathBuf>,
    generation: u64,
}

impl Pwm {
    /// A handle under a sysfs root (`/sys`, or a fake tree in tests). Nothing is touched yet.
    pub fn new(root: &Path) -> Self {
        Pwm {
            root: root.to_path_buf(),
            channel: "pwm2".into(),
            path: None,
            generation: 0,
        }
    }

    /// Increments every time a device path is (re)resolved, so a caller can tell that the
    /// board was replaced even if it was never observed absent.
    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// The attribute's current path, re-scanning `class/hwmon` if the cached one is gone.
    pub fn path(&mut self) -> Option<&Path> {
        if self.path.as_ref().is_none_or(|p| !p.exists()) {
            let found = find_device(&self.root, DEVICE_NAME).map(|d| d.join(&self.channel));
            if found.is_some() {
                self.generation += 1;
            }
            self.path = found;
        }
        self.path.as_deref()
    }

    /// Write one byte. NotFound when the board is absent; any error drops the cached path.
    pub fn write(&mut self, value: u8) -> io::Result<()> {
        let Some(path) = self.path().map(Path::to_path_buf) else {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("{DEVICE_NAME} {} not present", self.channel),
            ));
        };
        fs::write(&path, value.to_string()).inspect_err(|_| self.path = None)
    }

    /// The value the device reports, or None if absent or unreadable.
    pub fn read(&mut self) -> Option<u8> {
        let path = self.path()?.to_path_buf();
        match fs::read_to_string(&path) {
            Ok(text) => text.trim().parse().ok(),
            Err(_) => {
                self.path = None; // the device may have gone; re-scan next time
                None
            }
        }
    }

    /// Human-readable location, for `fanctl check` and the state file.
    pub fn describe(&mut self) -> String {
        let channel = self.channel.clone();
        match self.path() {
            Some(p) => format!("{DEVICE_NAME} {channel} at {}", p.display()),
            None => format!("{DEVICE_NAME} {channel}: not present"),
        }
    }
}
