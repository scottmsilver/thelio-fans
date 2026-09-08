use fanctl::pwm::{find_device, Pwm};
use std::fs;
use std::path::{Path, PathBuf};

fn make(root: &Path, hw: &str) -> PathBuf {
    let d = root.join("class/hwmon").join(hw);
    fs::create_dir_all(&d).unwrap();
    fs::write(d.join("name"), "system76_io\n").unwrap();
    fs::write(d.join("pwm2"), "76\n").unwrap();
    d
}

#[test]
fn find_device_by_name() {
    let dir = tempfile::tempdir().unwrap();
    let other = dir.path().join("class/hwmon/hwmon1");
    fs::create_dir_all(&other).unwrap();
    fs::write(other.join("name"), "coretemp\n").unwrap();
    assert!(find_device(dir.path(), "system76_io").is_none());
    let d = make(dir.path(), "hwmon2");
    assert_eq!(find_device(dir.path(), "system76_io"), Some(d));
}

#[test]
fn write_and_read_back() {
    let dir = tempfile::tempdir().unwrap();
    let d = make(dir.path(), "hwmon2");
    let mut pwm = Pwm::new(dir.path());
    pwm.write(128).unwrap();
    assert_eq!(fs::read_to_string(d.join("pwm2")).unwrap(), "128");
    assert_eq!(pwm.read(), Some(128));
}

#[test]
fn reresolves_after_renumbering() {
    let dir = tempfile::tempdir().unwrap();
    let d = make(dir.path(), "hwmon2");
    let mut pwm = Pwm::new(dir.path());
    pwm.write(100).unwrap();
    fs::rename(&d, d.with_file_name("hwmon7")).unwrap();
    pwm.write(110).unwrap();
    assert_eq!(
        fs::read_to_string(dir.path().join("class/hwmon/hwmon7/pwm2")).unwrap(),
        "110"
    );
}

#[test]
fn missing_device_errors_and_read_error_invalidates_path() {
    let dir = tempfile::tempdir().unwrap();
    fs::create_dir_all(dir.path().join("class/hwmon")).unwrap();
    let mut pwm = Pwm::new(dir.path());
    assert_eq!(pwm.read(), None);
    assert!(pwm.write(100).is_err());
    let d = make(dir.path(), "hwmon2");
    assert_eq!(pwm.read(), Some(76));
    fs::remove_file(d.join("pwm2")).unwrap();
    assert_eq!(pwm.read(), None);
    fs::write(d.join("pwm2"), "10").unwrap();
    assert_eq!(pwm.read(), Some(10));
}
