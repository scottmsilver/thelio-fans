//! Temperature inputs: CPU package from coretemp in sysfs, GPU through NVML.

use std::fs;
use std::path::Path;

/// One tick's temperature readings. Either may be missing.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Temps {
    /// CPU package temperature from coretemp.
    pub cpu_c: Option<f64>,
    /// GPU core temperature from NVML.
    pub gpu_c: Option<f64>,
}

impl Temps {
    /// The hotter of the two, or whichever is present.
    pub fn max_c(&self) -> Option<f64> {
        match (self.cpu_c, self.gpu_c) {
            (Some(c), Some(g)) => Some(c.max(g)),
            (c, g) => c.or(g),
        }
    }
}

fn millidegrees(path: &Path) -> Option<f64> {
    fs::read_to_string(path)
        .ok()?
        .trim()
        .parse::<i64>()
        .ok()
        .map(|v| v as f64 / 1000.0)
}

/// The coretemp package reading; if it has no package sensor, the hottest core.
pub fn cpu_package_c(root: &Path) -> Option<f64> {
    let mut dirs: Vec<_> = fs::read_dir(root.join("class/hwmon"))
        .ok()?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .collect();
    dirs.sort();
    for hw in dirs {
        if !fs::read_to_string(hw.join("name")).is_ok_and(|n| n.trim() == "coretemp") {
            continue;
        }
        let mut labels: Vec<_> = fs::read_dir(&hw)
            .ok()?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| {
                let n = p
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                n.starts_with("temp") && n.ends_with("_label")
            })
            .collect();
        labels.sort();
        let mut cores: Vec<f64> = Vec::new();
        for label in labels {
            let Ok(text) = fs::read_to_string(&label) else {
                continue;
            };
            let input = label.with_file_name(
                label
                    .file_name()
                    .unwrap()
                    .to_string_lossy()
                    .replace("_label", "_input"),
            );
            let Some(value) = millidegrees(&input) else {
                continue;
            };
            if text.trim().starts_with("Package") {
                return Some(value);
            }
            cores.push(value);
        }
        if let Some(max) = cores.into_iter().reduce(f64::max) {
            return Some(max);
        }
    }
    None
}

/// Source of the GPU temperature; a trait so the service can be tested without NVML.
pub trait GpuReader {
    /// The GPU core temperature in °C, or None if unavailable this tick.
    fn temp_c(&mut self) -> Option<f64>;
}

/// NVML session kept open across reads. Initialising it costs tens of milliseconds, so it
/// is done once; any error drops the session and the next read re-initialises.
#[derive(Default)]
pub struct NvmlGpu {
    nvml: Option<nvml_wrapper::Nvml>,
}

impl GpuReader for NvmlGpu {
    fn temp_c(&mut self) -> Option<f64> {
        if self.nvml.is_none() {
            self.nvml = nvml_wrapper::Nvml::init().ok();
        }
        let reading = self.nvml.as_ref().and_then(|nvml| {
            nvml.device_by_index(0)
                .ok()?
                .temperature(nvml_wrapper::enum_wrappers::device::TemperatureSensor::Gpu)
                .ok()
        });
        match reading {
            Some(t) => Some(t as f64),
            None => {
                self.nvml = None; // stale handle after a driver restart: start over next time
                None
            }
        }
    }
}

/// A reader that never reports a GPU, for machines without one or for tests.
pub struct NoGpu;

impl GpuReader for NoGpu {
    fn temp_c(&mut self) -> Option<f64> {
        None
    }
}

/// Both inputs for one tick.
pub fn read_temps(root: &Path, gpu: &mut dyn GpuReader) -> Temps {
    Temps {
        cpu_c: cpu_package_c(root),
        gpu_c: gpu.temp_c(),
    }
}
