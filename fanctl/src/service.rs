//! One control tick: read sensors, decide, write the PWM, record state.

use crate::config::Config;
use crate::controller::{pct_to_pwm, Controller, Decision};
use crate::pwm::Pwm;
use crate::sensors::{read_temps, GpuReader, Temps};
use crate::state::write_state;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

/// The service loop's state: sensors, controller, PWM handle, failure counters and the
/// last state written. `main.rs` calls [`Service::tick`] once per interval.
pub struct Service {
    cfg: Config,
    root: PathBuf,
    gpu: Box<dyn GpuReader>,
    dry_run: bool,
    controller: Controller,
    pwm: Pwm,
    write_failures: u32,
    device_present: bool,
    seen_generation: u64,
    pending_kick: bool,
    ready: bool,
    last_temps: Temps,
    last_error: Option<String>,
}

impl Service {
    /// A service under a sysfs root with the given GPU source. In `dry_run` decisions are
    /// made and recorded but nothing is written to the device.
    pub fn new(cfg: Config, root: PathBuf, gpu: Box<dyn GpuReader>, dry_run: bool) -> Self {
        Service {
            controller: Controller::new(cfg.clone()),
            pwm: Pwm::new(&root),
            cfg,
            root,
            gpu,
            dry_run,
            write_failures: 0,
            device_present: false,
            seen_generation: 0,
            pending_kick: false,
            ready: false,
            last_temps: Temps {
                cpu_c: None,
                gpu_c: None,
            },
            last_error: None,
        }
    }

    /// The control law, for callers that need to `kick` it or inspect it in tests.
    pub fn controller_mut(&mut self) -> &mut Controller {
        &mut self.controller
    }
    /// Consecutive failed writes with the device present.
    pub fn write_failures(&self) -> u32 {
        self.write_failures
    }
    /// True once control has actually been applied (or, in a dry run, decided).
    pub fn ready(&self) -> bool {
        self.ready
    }
    /// The readings from the most recent tick.
    pub fn last_temps(&self) -> Temps {
        self.last_temps
    }
    /// Seconds between ticks, from the config.
    pub fn interval_s(&self) -> f64 {
        self.cfg.interval_s
    }

    /// Returns Err only when writes have failed `write_failures_to_exit` times in a row
    /// with the device present.
    pub fn tick(&mut self, now: f64) -> Result<Decision, String> {
        let temps = read_temps(&self.root, self.gpu.as_mut());
        self.last_temps = temps;
        // The CPU reading is the safety input: without it we panic even if the GPU is cool.
        let temp = if temps.cpu_c.is_none() {
            None
        } else {
            temps.max_c()
        };
        let present = self.pwm.path().is_some();
        let generation = self.pwm.generation();
        if present && (!self.device_present || generation != self.seen_generation) {
            // Startup, or the board came back (even between two ticks): it may be stopped.
            self.pending_kick = true;
        }
        self.device_present = present;
        self.seen_generation = generation;
        if self.pending_kick {
            self.controller.kick(now); // re-armed every tick until a write actually lands
        }
        // Readback only means something when we are the writer.
        let readback = if self.dry_run { None } else { self.pwm.read() };
        let decision = self.controller.step(temp, now, readback);
        if decision.write && !self.dry_run {
            if !present {
                self.controller.write_failed(); // hold; retry when the board is back
                self.last_error = Some("device absent".into());
            } else {
                match self.pwm.write(decision.pwm) {
                    Ok(()) => {
                        self.write_failures = 0;
                        self.last_error = None;
                        self.ready = true;
                        self.pending_kick = false; // delivered: the hold now runs from here
                    }
                    Err(e) => {
                        self.controller.write_failed();
                        self.write_failures += 1;
                        self.last_error = Some(e.to_string());
                        if self.write_failures >= self.cfg.write_failures_to_exit {
                            return Err(format!(
                                "pwm write failed {} times: {e}",
                                self.write_failures
                            ));
                        }
                    }
                }
            }
        } else if self.dry_run {
            self.ready = true;
            self.pending_kick = false;
        }
        let unix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        write_state(
            &self.cfg.state_path,
            &serde_json::json!({
                "time": unix,
                "cpu_c": temps.cpu_c,
                "gpu_c": temps.gpu_c,
                "smoothed_c": decision.smoothed_c,
                "duty_pct": (decision.duty_pct * 10.0).round() / 10.0,
                "pwm": decision.pwm,
                "reason": decision.reason,
                "panic": decision.panic,
                "dry_run": self.dry_run,
                "device": self.pwm.describe(),
                "error": self.last_error,
            }),
        );
        Ok(decision)
    }

    /// No auto mode exists in this driver, so leave the fan at a safe duty.
    pub fn shutdown(&mut self) {
        if self.dry_run {
            return;
        }
        if let Err(e) = self.pwm.write(pct_to_pwm(self.cfg.exit_duty_pct)) {
            eprintln!("fanctl: could not set exit duty: {e}");
        }
    }
}
