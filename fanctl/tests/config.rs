use fanctl::config::{load, Config};
use std::fs;

fn write(dir: &tempfile::TempDir, text: &str) -> std::path::PathBuf {
    let p = dir.path().join("fanctl.toml");
    fs::write(&p, text).unwrap();
    p
}

#[test]
fn defaults_match_spec() {
    let c = Config::default();
    assert_eq!(
        c.curve,
        vec![
            (50.0, 25.0),
            (58.0, 30.0),
            (65.0, 40.0),
            (75.0, 55.0),
            (82.0, 75.0),
            (88.0, 100.0)
        ]
    );
    assert_eq!((c.floor_pct, c.ema_seconds, c.deadband_c), (25.0, 8.0, 2.0));
    assert_eq!(
        (c.spindown_delay_s, c.max_decrease_pct_per_s, c.reassert_s),
        (30.0, 3.0, 60.0)
    );
    assert_eq!(
        (
            c.sensor_failures_to_panic,
            c.write_failures_to_exit,
            c.exit_duty_pct
        ),
        (5, 10, 50.0)
    );
    assert_eq!(
        (c.start_pct, c.start_seconds, c.interval_s),
        (45.0, 3.0, 1.0)
    );
}

#[test]
fn missing_file_gives_defaults() {
    assert_eq!(load(None).unwrap(), Config::default());
    assert_eq!(
        load(Some(std::path::Path::new("/nonexistent/fanctl.toml"))).unwrap(),
        Config::default()
    );
}

#[test]
fn file_overrides() {
    let dir = tempfile::tempdir().unwrap();
    let p = write(&dir, "floor_pct = 30\ncurve = [[40, 30], [90, 100]]\n");
    let c = load(Some(&p)).unwrap();
    assert_eq!(c.floor_pct, 30.0);
    assert_eq!(c.curve, vec![(40.0, 30.0), (90.0, 100.0)]);
}

#[test]
fn bad_and_unsafe_values_are_rejected() {
    let cases = [
        "curve = [[50, 30]]",
        "curve = [[50, 30], [40, 40]]",
        "curve = [[50, 60], [60, 40]]",
        "curve = [[50, 30], [60, 140]]",
        "curve = [[nan, 25], [60, 40]]",
        "floor_pct = -1",
        "floor_pct = 0",
        "exit_duty_pct = 0",
        "start_pct = 20",
        "start_seconds = -1",
        "ema_seconds = 0",
        "ema_seconds = inf",
        "spindown_delay_s = inf",
        "interval_s = 0.05",
        "interval_s = 60",
        "bogus = 1",
        "floor_pct = 'high'",
    ];
    for text in cases {
        let dir = tempfile::tempdir().unwrap();
        let p = write(&dir, text);
        assert!(load(Some(&p)).is_err(), "accepted: {text}");
    }
}
