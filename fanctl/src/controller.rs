//! The control law. Pure: no clocks, no files. Everything comes in through `step`.
//!
//! Behaviours borrowed, with thanks, from other fan controllers (behaviour only, no code):
//! linear point curve and spin-down delay from LACT (MIT); exponential smoothing from
//! fan2go and CoolerControl; temperature deadband from LACT and CoolerControl;
//! decrease-only slew limit and periodic re-assert from CoolerControl; missing sensor
//! means full speed from CoolerControl; start kick from fancontrol and CoolerControl.

use crate::config::Config;

/// The temperature assumed once the CPU sensor has been missing for too long.
pub const PANIC_TEMP_C: f64 = 100.0;

/// What one tick decided. The caller writes `pwm` only when `write` is set.
#[derive(Debug, Clone, PartialEq)]
pub struct Decision {
    /// Duty now applied, in percent, after kick, spin-down delay and slew limit.
    pub duty_pct: f64,
    /// `duty_pct` as the 0..=255 byte the driver takes.
    pub pwm: u8,
    /// Whether the byte should be written this tick (changed, re-assert or mismatch).
    pub write: bool,
    /// Comma-separated list of what happened, e.g. `kick, increase, changed` or `steady`.
    pub reason: String,
    /// The smoothed input temperature, or None until the first reading.
    pub smoothed_c: Option<f64>,
    /// True while the CPU sensor is considered lost and the duty is forced to 100 %.
    pub panic: bool,
}

/// Linear interpolation over (temperature, duty) points, clamped at both ends.
pub fn interpolate(curve: &[(f64, f64)], temp_c: f64) -> f64 {
    let (first_t, first_d) = curve[0];
    if temp_c <= first_t {
        return first_d;
    }
    for w in curve.windows(2) {
        let ((t0, d0), (t1, d1)) = (w[0], w[1]);
        if temp_c <= t1 {
            return d0 + (d1 - d0) * (temp_c - t0) / (t1 - t0);
        }
    }
    curve[curve.len() - 1].1
}

/// Percent to the driver's 0..=255 byte, rounded and clamped.
pub fn pct_to_pwm(pct: f64) -> u8 {
    (pct * 255.0 / 100.0).round().clamp(0.0, 255.0) as u8
}

/// The control law's state between ticks. Feed it one `step` per tick with the input
/// temperature and the PWM read back from the device.
pub struct Controller {
    cfg: Config,
    ema: Option<f64>,
    applied_temp: Option<f64>,
    curve_pct: f64,
    applied_pct: f64,
    last_time: Option<f64>,
    last_increase_at: f64,
    last_write_at: f64,
    last_written_pwm: Option<u8>,
    sensor_failures: u32,
    kick_until: f64,
}

impl Controller {
    /// A controller at the floor duty with no history.
    pub fn new(cfg: Config) -> Self {
        Controller {
            ema: None,
            applied_temp: None,
            curve_pct: cfg.floor_pct,
            applied_pct: cfg.floor_pct,
            last_time: None,
            last_increase_at: f64::NEG_INFINITY,
            last_write_at: f64::NEG_INFINITY,
            last_written_pwm: None,
            sensor_failures: 0,
            kick_until: f64::NEG_INFINITY,
            cfg,
        }
    }

    /// Hold at least `start_pct` for `start_seconds`: the fan may be stopped and a low
    /// duty need not start it.
    pub fn kick(&mut self, now: f64) {
        self.kick_until = now + self.cfg.start_seconds;
    }

    /// The caller could not apply the last decision; retry on the next tick.
    pub fn write_failed(&mut self) {
        self.last_written_pwm = None;
    }

    fn smooth(&mut self, temp_c: Option<f64>, dt: f64) -> (Option<f64>, bool) {
        match temp_c {
            None => {
                self.sensor_failures = self.sensor_failures.saturating_add(1);
                if self.sensor_failures >= self.cfg.sensor_failures_to_panic {
                    (Some(PANIC_TEMP_C), true)
                } else {
                    (self.ema, false)
                }
            }
            Some(t) => {
                self.sensor_failures = 0;
                let ema = match self.ema {
                    Some(e) if dt > 0.0 => {
                        let alpha = 1.0 - (-dt / self.cfg.ema_seconds).exp();
                        e + alpha * (t - e)
                    }
                    _ => t,
                };
                self.ema = Some(ema);
                (Some(ema), false)
            }
        }
    }

    /// One tick. `temp_c` is the input (None when the CPU sensor is missing), `now` a
    /// monotonic time in seconds, `readback_pwm` what the device currently reports.
    pub fn step(&mut self, temp_c: Option<f64>, now: f64, readback_pwm: Option<u8>) -> Decision {
        let dt = self.last_time.map_or(0.0, |t| (now - t).max(0.0));
        self.last_time = Some(now);
        let (smoothed, panic) = self.smooth(temp_c, dt);
        let mut reasons: Vec<&str> = Vec::new();

        // Deadband: re-evaluate the curve only when the smoothed temperature has moved enough.
        if panic {
            self.curve_pct = 100.0; // not through the curve: a curve may top out below 100
            self.applied_temp = smoothed;
        } else if let Some(s) = smoothed {
            let moved = self
                .applied_temp
                .is_none_or(|a| (s - a).abs() >= self.cfg.deadband_c);
            if moved {
                self.curve_pct = interpolate(&self.cfg.curve, s).max(self.cfg.floor_pct);
                self.applied_temp = Some(s);
            }
        }
        // While kicking, the target is raised to start_pct; afterwards it is the curve again.
        let kicking = now < self.kick_until;
        let mut target = self.curve_pct;
        let mut kicked = false;
        if kicking && target < self.cfg.start_pct {
            target = self.cfg.start_pct;
            kicked = true;
            reasons.push("kick");
        }

        // Move the applied duty toward the target: up at once, down slowly and not too soon.
        if target > self.applied_pct {
            self.applied_pct = target;
            if !kicked {
                self.last_increase_at = now; // a kick is not a thermal increase: no hold after it
            }
            reasons.push("increase");
        } else if target < self.applied_pct && !kicking {
            if now - self.last_increase_at < self.cfg.spindown_delay_s {
                reasons.push("spin-down delay");
            } else {
                let budget = self.cfg.max_decrease_pct_per_s * dt;
                self.applied_pct = (self.applied_pct - budget).max(target);
                reasons.push("decrease");
            }
        }

        let pwm = pct_to_pwm(self.applied_pct);
        // The Io firmware keeps duty in tenths of a percent, so a read-back can differ from
        // the written byte by one. Anything further is someone else's write.
        let foreign_write = matches!(
            (readback_pwm, self.last_written_pwm),
            (Some(read), Some(written)) if read.abs_diff(written) > 1
        );
        let mut write = false;
        if Some(pwm) != self.last_written_pwm {
            write = true;
            reasons.push("changed");
        } else if foreign_write {
            write = true;
            reasons.push("readback mismatch");
        } else if now - self.last_write_at >= self.cfg.reassert_s {
            write = true;
            reasons.push("reassert");
        }
        if write {
            self.last_written_pwm = Some(pwm);
            self.last_write_at = now;
        }
        if panic {
            reasons.insert(0, "sensor missing: full speed");
        }
        Decision {
            duty_pct: self.applied_pct,
            pwm,
            write,
            reason: if reasons.is_empty() {
                "steady".into()
            } else {
                reasons.join(", ")
            },
            smoothed_c: smoothed,
            panic,
        }
    }
}
