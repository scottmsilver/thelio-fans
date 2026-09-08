use fanctl::config::Config;
use fanctl::controller::{interpolate, pct_to_pwm, Controller};

fn cfg() -> Config {
    Config::default()
}

fn unsmoothed() -> Config {
    Config {
        ema_seconds: 0.001,
        ..Config::default()
    }
}

#[test]
fn interpolation_and_clamping() {
    let curve = cfg().curve;
    assert_eq!(interpolate(&curve, 30.0), 25.0);
    assert_eq!(interpolate(&curve, 50.0), 25.0);
    assert_eq!(interpolate(&curve, 54.0), 27.5);
    assert_eq!(interpolate(&curve, 88.0), 100.0);
    assert_eq!(interpolate(&curve, 120.0), 100.0);
    assert_eq!(
        (pct_to_pwm(0.0), pct_to_pwm(100.0), pct_to_pwm(25.0)),
        (0, 255, 64)
    );
}

#[test]
fn first_sample_writes_floor_at_idle() {
    let mut c = Controller::new(cfg());
    let d = c.step(Some(40.0), 0.0, None);
    assert!(d.write && d.duty_pct == 25.0 && d.pwm == 64 && !d.panic);
}

#[test]
fn ema_smooths_a_spike_and_deadband_suppresses_jitter() {
    let mut c = Controller::new(cfg());
    c.step(Some(40.0), 0.0, Some(64));
    let d = c.step(Some(70.0), 1.0, Some(64));
    let s = d.smoothed_c.unwrap();
    assert!(s > 40.0 && s < 45.0);
    assert_eq!(d.duty_pct, 25.0);
    let mut last = d;
    for t in 2..8 {
        last = c.step(Some(40.0 + (t % 2) as f64), t as f64, Some(64));
    }
    assert!(last.duty_pct == 25.0 && !last.write);
}

#[test]
fn increase_is_immediate_and_decrease_waits_then_slews() {
    let mut c = Controller::new(unsmoothed());
    c.step(Some(40.0), 0.0, Some(64));
    let up = c.step(Some(82.0), 1.0, Some(64));
    assert!(up.duty_pct == 75.0 && up.write && up.reason.contains("increase"));
    let hold = c.step(Some(40.0), 10.0, Some(up.pwm));
    assert!(hold.duty_pct == 75.0 && hold.reason.contains("spin-down delay"));
    assert_eq!(c.step(Some(40.0), 30.0, Some(up.pwm)).duty_pct, 75.0);
    let first = c.step(Some(40.0), 31.0, Some(up.pwm));
    assert!((first.duty_pct - 72.0).abs() < 1e-9 && first.write);
    let later = c.step(Some(40.0), 41.0, Some(first.pwm));
    assert!((later.duty_pct - 42.0).abs() < 1e-9);
    assert_eq!(c.step(Some(40.0), 100.0, Some(later.pwm)).duty_pct, 25.0);
}

#[test]
fn reassert_and_readback_mismatch_force_a_write() {
    let mut c = Controller::new(cfg());
    c.step(Some(40.0), 0.0, None);
    assert!(!c.step(Some(40.0), 1.0, Some(64)).write);
    assert!(c.step(Some(40.0), 61.0, Some(64)).write);
    let d = c.step(Some(40.0), 62.0, Some(200));
    assert!(d.write && d.reason.contains("readback"));
}

#[test]
fn missing_sensor_holds_then_panics_to_full_speed() {
    let mut c = Controller::new(cfg());
    c.step(Some(40.0), 0.0, Some(64));
    for t in 1..5 {
        let d = c.step(None, t as f64, Some(64));
        assert!(d.duty_pct == 25.0 && !d.panic);
    }
    let d = c.step(None, 5.0, Some(64));
    assert!(d.panic && d.duty_pct == 100.0 && d.pwm == 255 && d.write);
    let back = c.step(Some(40.0), 6.0, Some(255));
    assert!(!back.panic && back.duty_pct == 100.0);
}

#[test]
fn panic_forces_full_duty_regardless_of_curve() {
    let mut c = Controller::new(Config {
        curve: vec![(50.0, 25.0), (90.0, 60.0)],
        ..Config::default()
    });
    c.step(Some(40.0), 0.0, None);
    let mut d = c.step(None, 1.0, Some(64));
    for t in 2..6 {
        d = c.step(None, t as f64, Some(64));
    }
    assert!(d.panic && d.duty_pct == 100.0 && d.pwm == 255);
}

#[test]
fn start_kick_holds_start_duty_then_returns_to_curve() {
    let mut c = Controller::new(unsmoothed());
    c.kick(0.0);
    let d = c.step(Some(40.0), 0.0, None);
    assert!(d.duty_pct == 45.0 && d.write && d.reason.contains("kick"));
    assert_eq!(c.step(Some(40.0), 2.9, Some(d.pwm)).duty_pct, 45.0);
    // The kick is not an "increase": once it ends the duty ramps down without the delay.
    let soon = c.step(Some(40.0), 5.0, Some(d.pwm));
    assert!(soon.duty_pct < 45.0 && soon.reason.contains("decrease"));
    let after = c.step(Some(40.0), 40.0, Some(d.pwm));
    assert!(after.duty_pct < 45.0);
    assert_eq!(c.step(Some(40.0), 60.0, Some(after.pwm)).duty_pct, 25.0);
}

#[test]
fn kick_never_lowers_a_hot_target() {
    let mut c = Controller::new(unsmoothed());
    c.kick(0.0);
    assert!(c.step(Some(85.0), 0.0, None).duty_pct > 45.0);
}

#[test]
fn write_failed_causes_retry_next_tick() {
    let mut c = Controller::new(cfg());
    assert!(c.step(Some(40.0), 0.0, None).write);
    c.write_failed();
    assert!(c.step(Some(40.0), 1.0, None).write);
}

#[test]
fn readback_off_by_one_is_not_a_mismatch() {
    // The Io firmware stores duty in tenths of a percent: writing 64 reads back as 63.
    let mut c = Controller::new(cfg());
    assert_eq!(c.step(Some(40.0), 0.0, None).pwm, 64);
    let d = c.step(Some(40.0), 1.0, Some(63));
    assert!(!d.write, "{}", d.reason);
    assert!(c.step(Some(40.0), 2.0, Some(60)).write);
}
