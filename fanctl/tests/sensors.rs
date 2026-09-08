use fanctl::sensors::{cpu_package_c, read_temps, GpuReader};
use std::fs;
use std::path::Path;

struct FakeGpu(Option<f64>);
impl GpuReader for FakeGpu {
    fn temp_c(&mut self) -> Option<f64> {
        self.0
    }
}

fn coretemp(root: &Path, package: Option<&str>) {
    let d = root.join("class/hwmon/hwmon5");
    fs::create_dir_all(&d).unwrap();
    fs::write(d.join("name"), "coretemp").unwrap();
    fs::write(d.join("temp2_label"), "Core 0").unwrap();
    fs::write(d.join("temp2_input"), "70000").unwrap();
    if let Some(p) = package {
        fs::write(d.join("temp1_label"), "Package id 0").unwrap();
        fs::write(d.join("temp1_input"), p).unwrap();
    }
}

#[test]
fn cpu_package_prefers_package_label_and_falls_back_to_hottest_core() {
    let dir = tempfile::tempdir().unwrap();
    coretemp(dir.path(), Some("56000"));
    assert_eq!(cpu_package_c(dir.path()), Some(56.0));
    let dir2 = tempfile::tempdir().unwrap();
    coretemp(dir2.path(), None);
    assert_eq!(cpu_package_c(dir2.path()), Some(70.0));
}

#[test]
fn max_of_cpu_and_gpu_and_missing_handling() {
    let dir = tempfile::tempdir().unwrap();
    coretemp(dir.path(), Some("56000"));
    assert_eq!(
        read_temps(dir.path(), &mut FakeGpu(Some(61.0))).max_c(),
        Some(61.0)
    );
    assert_eq!(
        read_temps(dir.path(), &mut FakeGpu(None)).max_c(),
        Some(56.0)
    );
    let t = read_temps(&dir.path().join("nowhere"), &mut FakeGpu(None));
    assert!(t.cpu_c.is_none() && t.max_c().is_none());
}
