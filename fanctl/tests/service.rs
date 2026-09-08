use fanctl::config::Config;
use fanctl::sensors::GpuReader;
use fanctl::service::Service;
use std::fs;
use std::path::{Path, PathBuf};

struct FakeGpu(Option<f64>);
impl GpuReader for FakeGpu {
    fn temp_c(&mut self) -> Option<f64> {
        self.0
    }
}

fn sysfs(root: &Path, temp: &str) -> PathBuf {
    let io = root.join("class/hwmon/hwmon2");
    fs::create_dir_all(&io).unwrap();
    fs::write(io.join("name"), "system76_io").unwrap();
    fs::write(io.join("pwm2"), "76").unwrap();
    let ct = root.join("class/hwmon/hwmon5");
    fs::create_dir_all(&ct).unwrap();
    fs::write(ct.join("name"), "coretemp").unwrap();
    fs::write(ct.join("temp1_label"), "Package id 0").unwrap();
    fs::write(ct.join("temp1_input"), temp).unwrap();
    io.join("pwm2")
}

fn service(root: &Path, dry_run: bool, cfg: Config) -> Service {
    let cfg = Config {
        state_path: root.join("state.json"),
        ..cfg
    };
    Service::new(cfg, root.to_path_buf(), Box::new(FakeGpu(None)), dry_run)
}

#[test]
fn writes_kick_then_floor_and_state() {
    let dir = tempfile::tempdir().unwrap();
    let pwm = sysfs(dir.path(), "40000");
    let mut svc = service(dir.path(), false, Config::default());
    let d = svc.tick(0.0).unwrap();
    assert!(d.write && d.reason.contains("kick"));
    assert_eq!(fs::read_to_string(&pwm).unwrap(), "115"); // 45 % start kick
    let state: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(dir.path().join("state.json")).unwrap()).unwrap();
    assert_eq!(state["cpu_c"], 40.0);
    assert_eq!(state["pwm"], 115);
    let settled = svc.tick(60.0).unwrap();
    assert_eq!(settled.duty_pct, 25.0);
    assert_eq!(fs::read_to_string(&pwm).unwrap(), "64");
}

#[test]
fn dry_run_never_writes_and_ignores_readback() {
    let dir = tempfile::tempdir().unwrap();
    let pwm = sysfs(dir.path(), "40000");
    let mut svc = service(dir.path(), true, Config::default());
    svc.tick(0.0).unwrap();
    assert_eq!(fs::read_to_string(&pwm).unwrap(), "76");
    let second = svc.tick(1.0).unwrap();
    assert!(!second.reason.contains("readback"));
}

#[test]
fn shutdown_writes_exit_duty() {
    let dir = tempfile::tempdir().unwrap();
    let pwm = sysfs(dir.path(), "40000");
    let mut svc = service(dir.path(), false, Config::default());
    svc.tick(0.0).unwrap();
    svc.shutdown();
    assert_eq!(fs::read_to_string(&pwm).unwrap(), "128");
}

#[test]
fn write_failures_eventually_error() {
    let dir = tempfile::tempdir().unwrap();
    let pwm = sysfs(dir.path(), "40000");
    let mut svc = service(
        dir.path(),
        false,
        Config {
            write_failures_to_exit: 2,
            ..Config::default()
        },
    );
    svc.tick(0.0).unwrap();
    // Device present but unwritable: replace the attribute with a directory.
    fs::remove_file(&pwm).unwrap();
    fs::create_dir(&pwm).unwrap();
    assert!(svc.tick(61.0).is_ok()); // first failure (reassert due)
    svc.controller_mut().write_failed();
    assert!(svc.tick(62.0).is_err());
}

#[test]
fn cpu_loss_panics_even_with_cool_gpu() {
    let dir = tempfile::tempdir().unwrap();
    let pwm = sysfs(dir.path(), "40000");
    fs::remove_file(dir.path().join("class/hwmon/hwmon5/temp1_input")).unwrap();
    let cfg = Config {
        state_path: dir.path().join("state.json"),
        ..Config::default()
    };
    let mut svc = Service::new(
        cfg,
        dir.path().to_path_buf(),
        Box::new(FakeGpu(Some(35.0))),
        false,
    );
    let mut d = svc.tick(0.0).unwrap();
    for t in 1..5 {
        d = svc.tick(t as f64).unwrap();
    }
    assert!(d.panic && d.pwm == 255);
    assert_eq!(fs::read_to_string(&pwm).unwrap(), "255");
}

#[test]
fn absent_device_holds_without_counting_and_kicks_on_return() {
    let dir = tempfile::tempdir().unwrap();
    let pwm = sysfs(dir.path(), "40000");
    let device = pwm.parent().unwrap().to_path_buf();
    let mut svc = service(
        dir.path(),
        false,
        Config {
            write_failures_to_exit: 2,
            ..Config::default()
        },
    );
    svc.tick(0.0).unwrap();
    let gone = device.with_file_name("gone");
    fs::rename(&device, &gone).unwrap();
    fs::write(gone.join("name"), "other").unwrap();
    for t in 1..10 {
        svc.tick(t as f64).unwrap();
    }
    assert_eq!(svc.write_failures(), 0);
    fs::rename(&gone, &device).unwrap();
    fs::write(device.join("name"), "system76_io").unwrap();
    let back = svc.tick(100.0).unwrap();
    assert!(back.reason.contains("kick"));
    assert_eq!(fs::read_to_string(&pwm).unwrap(), "115");
}

#[test]
fn reconnection_between_ticks_still_kicks() {
    let dir = tempfile::tempdir().unwrap();
    let pwm = sysfs(dir.path(), "40000");
    let device = pwm.parent().unwrap().to_path_buf();
    let mut svc = service(dir.path(), false, Config::default());
    svc.tick(0.0).unwrap();
    svc.tick(60.0).unwrap(); // settled on the floor
                             // Board re-enumerates between ticks: new hwmon number, never observed absent.
    let renumbered = device.with_file_name("hwmon9");
    fs::rename(&device, &renumbered).unwrap();
    fs::write(renumbered.join("pwm2"), "0").unwrap();
    let back = svc.tick(61.0).unwrap();
    assert!(back.reason.contains("kick"), "{}", back.reason);
    assert_eq!(fs::read_to_string(renumbered.join("pwm2")).unwrap(), "115");
}

#[test]
fn kick_is_delivered_even_if_early_writes_fail() {
    let dir = tempfile::tempdir().unwrap();
    let pwm = sysfs(dir.path(), "40000");
    // Present but unwritable for the first ticks: the attribute is a directory.
    fs::remove_file(&pwm).unwrap();
    fs::create_dir(&pwm).unwrap();
    let mut svc = service(dir.path(), false, Config::default());
    for t in 0..8 {
        svc.tick(t as f64).unwrap();
    }
    fs::remove_dir(&pwm).unwrap();
    fs::write(&pwm, "0").unwrap();
    let first_success = svc.tick(8.0).unwrap();
    assert!(first_success.reason.contains("kick"));
    assert_eq!(fs::read_to_string(&pwm).unwrap(), "115");
    assert_eq!(svc.tick(10.0).unwrap().duty_pct, 45.0); // hold measured from delivery
}
