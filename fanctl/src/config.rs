//! Controller settings: defaults from the design spec, optional TOML overrides, validation.

use serde::Deserialize;
use std::fmt;
use std::path::{Path, PathBuf};

/// The fan must never be commanded to stop.
pub const MIN_FLOOR_PCT: f64 = 10.0;
/// The duty written on every exit must move real air, whatever the config says.
pub const MIN_EXIT_PCT: f64 = 25.0;
/// The unit's WatchdogSec is 30 s and TimeoutStopSec 10 s.
pub const MAX_INTERVAL_S: f64 = 5.0;

/// All tunables, one per key in `/etc/fanctl.toml`. Every field has a default; unknown
/// keys are rejected; [`Config::validate`] bounds each value.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    /// (temperature °C, duty %) points, linearly interpolated, clamped at the ends.
    pub curve: Vec<(f64, f64)>,
    /// Lowest duty ever commanded; the curve is clamped up to it so the fan never stops.
    pub floor_pct: f64,
    /// Time constant of the exponential moving average applied to the input temperature.
    pub ema_seconds: f64,
    /// The curve is re-evaluated only when the smoothed temperature moves this far.
    pub deadband_c: f64,
    /// After any increase, no decrease for this long.
    pub spindown_delay_s: f64,
    /// Slew limit on decreases; increases are immediate.
    pub max_decrease_pct_per_s: f64,
    /// Rewrite the PWM at least this often even when unchanged.
    pub reassert_s: f64,
    /// Consecutive ticks without a CPU reading before the duty is forced to 100 %.
    pub sensor_failures_to_panic: u32,
    /// Consecutive failed writes, with the device present, before the service exits.
    pub write_failures_to_exit: u32,
    /// Duty written on every exit path and by `fanctl safe`.
    pub exit_duty_pct: f64,
    /// Duty applied for `start_seconds` at startup and whenever the board returns.
    pub start_pct: f64,
    /// Length of the start kick.
    pub start_seconds: f64,
    /// Seconds between ticks.
    pub interval_s: f64,
    /// Where the JSON status file for the dashboard is written.
    pub state_path: PathBuf,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            curve: vec![
                (50.0, 25.0),
                (58.0, 30.0),
                (65.0, 40.0),
                (75.0, 55.0),
                (82.0, 75.0),
                (88.0, 100.0),
            ],
            floor_pct: 25.0,
            ema_seconds: 8.0,
            deadband_c: 2.0,
            spindown_delay_s: 30.0,
            max_decrease_pct_per_s: 3.0,
            reassert_s: 60.0,
            sensor_failures_to_panic: 5,
            write_failures_to_exit: 10,
            exit_duty_pct: 50.0,
            start_pct: 45.0,
            start_seconds: 3.0,
            interval_s: 1.0,
            state_path: PathBuf::from("/run/fanctl/state.json"),
        }
    }
}

/// A configuration that failed to parse or validate, with a message naming the key.
#[derive(Debug)]
pub struct ConfigError(pub String);

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

fn err<T>(msg: impl Into<String>) -> Result<T, ConfigError> {
    Err(ConfigError(msg.into()))
}

impl Config {
    /// Reject non-finite numbers, non-monotonic curves and out-of-range values. The bounds
    /// are safety limits (the fan never stops, the exit duty moves air, a tick fits the
    /// watchdog), not preferences.
    pub fn validate(&self) -> Result<(), ConfigError> {
        let numbers: Vec<(&str, f64)> = vec![
            ("floor_pct", self.floor_pct),
            ("ema_seconds", self.ema_seconds),
            ("deadband_c", self.deadband_c),
            ("spindown_delay_s", self.spindown_delay_s),
            ("max_decrease_pct_per_s", self.max_decrease_pct_per_s),
            ("reassert_s", self.reassert_s),
            ("exit_duty_pct", self.exit_duty_pct),
            ("start_pct", self.start_pct),
            ("start_seconds", self.start_seconds),
            ("interval_s", self.interval_s),
        ];
        for (name, value) in numbers {
            if !value.is_finite() {
                return err(format!("{name} must be a finite number"));
            }
        }
        if self
            .curve
            .iter()
            .any(|(t, d)| !t.is_finite() || !d.is_finite())
        {
            return err("curve values must be finite numbers");
        }
        if self.curve.len() < 2 {
            return err("curve needs at least two points");
        }
        if self.curve.windows(2).any(|w| w[1].0 <= w[0].0) {
            return err("curve temperatures must strictly increase");
        }
        if self.curve.windows(2).any(|w| w[1].1 < w[0].1) {
            return err("curve duties must not decrease");
        }
        if self.curve.iter().any(|(_, d)| !(0.0..=100.0).contains(d)) {
            return err("curve duties must be between 0 and 100");
        }
        if !(MIN_FLOOR_PCT..=100.0).contains(&self.floor_pct) {
            return err(format!(
                "floor_pct must be between {MIN_FLOOR_PCT} and 100: the fan never stops"
            ));
        }
        if !(MIN_EXIT_PCT..=100.0).contains(&self.exit_duty_pct) {
            return err(format!(
                "exit_duty_pct must be between {MIN_EXIT_PCT} and 100"
            ));
        }
        if !(self.floor_pct..=100.0).contains(&self.start_pct) {
            return err("start_pct must be between floor_pct and 100");
        }
        if !(0.0..=30.0).contains(&self.start_seconds) {
            return err("start_seconds must be between 0 and 30");
        }
        if self.ema_seconds <= 0.0 || self.spindown_delay_s <= 0.0 || self.reassert_s <= 0.0 {
            return err("ema_seconds, spindown_delay_s and reassert_s must be positive");
        }
        if self.deadband_c < 0.0 || self.max_decrease_pct_per_s <= 0.0 {
            return err("deadband_c must be >= 0 and max_decrease_pct_per_s > 0");
        }
        if !(0.2..=MAX_INTERVAL_S).contains(&self.interval_s) {
            return err(format!(
                "interval_s must be between 0.2 and {MAX_INTERVAL_S} seconds"
            ));
        }
        if self.sensor_failures_to_panic < 1 || self.write_failures_to_exit < 1 {
            return err("failure counts must be at least 1");
        }
        Ok(())
    }
}

/// Defaults, overridden by the TOML file at `path` if it exists.
pub fn load(path: Option<&Path>) -> Result<Config, ConfigError> {
    let cfg = match path {
        Some(p) if p.exists() => {
            let text = std::fs::read_to_string(p)
                .map_err(|e| ConfigError(format!("{}: {e}", p.display())))?;
            toml::from_str::<Config>(&text)
                .map_err(|e| ConfigError(format!("{}: {e}", p.display())))?
        }
        _ => Config::default(),
    };
    cfg.validate()?;
    Ok(cfg)
}
