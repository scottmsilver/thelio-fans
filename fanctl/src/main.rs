//! fanctl: hold the Thelio intake fan at a smooth duty. Runs unprivileged as a systemd service.
//!
//!     fanctl run        the service loop (Type=notify, watchdog pings, safe duty on exit)
//!     fanctl dry-run    same loop, prints decisions, writes nothing
//!     fanctl check      print resolved config, device path, permissions, sensor readings
//!     fanctl safe       write the exit duty and quit (used by ExecStopPost)
//!
//! Options before the command: --config PATH (default /etc/fanctl.toml), --state PATH,
//! --sysfs PATH. run and dry-run accept --ticks N.

use fanctl::config::{load, Config};
use fanctl::controller::pct_to_pwm;
use fanctl::notify::sd_notify;
use fanctl::pwm::Pwm;
use fanctl::sensors::{read_temps, NvmlGpu};
use fanctl::service::Service;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_sig: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

struct Args {
    config: PathBuf,
    sysfs: PathBuf,
    state: Option<PathBuf>,
    command: String,
    ticks: Option<u64>,
}

fn usage() -> String {
    "usage: fanctl [--config PATH] [--sysfs PATH] [--state PATH] (run|dry-run|check|safe) [--ticks N]".into()
}

fn parse_args() -> Result<Args, String> {
    let mut args = Args {
        config: PathBuf::from("/etc/fanctl.toml"),
        sysfs: PathBuf::from("/sys"),
        state: None,
        command: String::new(),
        ticks: None,
    };
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--config" => args.config = PathBuf::from(it.next().ok_or_else(usage)?),
            "--sysfs" => args.sysfs = PathBuf::from(it.next().ok_or_else(usage)?),
            "--state" => args.state = Some(PathBuf::from(it.next().ok_or_else(usage)?)),
            "--ticks" => {
                args.ticks = Some(it.next().ok_or_else(usage)?.parse().map_err(|_| usage())?)
            }
            "run" | "dry-run" | "check" | "safe" if args.command.is_empty() => args.command = a,
            "-h" | "--help" => return Err(usage()),
            _ => return Err(format!("unknown argument {a}\n{}", usage())),
        }
    }
    if args.command.is_empty() {
        return Err(usage());
    }
    Ok(args)
}

fn check(cfg: &Config, root: &Path) -> ExitCode {
    println!("config:");
    println!("  curve = {:?}", cfg.curve);
    println!(
        "  floor_pct = {}  start_pct = {}  start_seconds = {}",
        cfg.floor_pct, cfg.start_pct, cfg.start_seconds
    );
    println!(
        "  ema_seconds = {}  deadband_c = {}  spindown_delay_s = {}",
        cfg.ema_seconds, cfg.deadband_c, cfg.spindown_delay_s
    );
    println!(
        "  max_decrease_pct_per_s = {}  reassert_s = {}  interval_s = {}",
        cfg.max_decrease_pct_per_s, cfg.reassert_s, cfg.interval_s
    );
    println!(
        "  sensor_failures_to_panic = {}  write_failures_to_exit = {}  exit_duty_pct = {}",
        cfg.sensor_failures_to_panic, cfg.write_failures_to_exit, cfg.exit_duty_pct
    );
    println!("  state_path = {}", cfg.state_path.display());
    let mut pwm = Pwm::new(root);
    println!("device: {}", pwm.describe());
    if let Some(path) = pwm.path().map(Path::to_path_buf) {
        if let Ok(md) = std::fs::metadata(&path) {
            let writable = std::fs::OpenOptions::new().write(true).open(&path).is_ok();
            println!(
                "  mode {:o} uid {} gid {} writable-by-me {}",
                md.mode() & 0o777,
                md.uid(),
                md.gid(),
                writable
            );
        }
        println!("  current pwm {:?}", pwm.read());
    }
    let temps = read_temps(root, &mut NvmlGpu::default());
    println!(
        "sensors: cpu {:?} gpu {:?} max {:?}",
        temps.cpu_c,
        temps.gpu_c,
        temps.max_c()
    );
    // SAFETY: getuid/getgid have no preconditions.
    println!(
        "running as uid {} gid {}",
        unsafe { libc::getuid() },
        unsafe { libc::getgid() }
    );
    ExitCode::SUCCESS
}

fn run_loop(svc: &mut Service, ticks: Option<u64>, verbose: bool) -> Result<(), String> {
    let mut done = 0u64;
    let mut announced = false;
    let interval = Duration::from_secs_f64(svc.interval_s());
    while !STOP.load(Ordering::SeqCst) && ticks.is_none_or(|n| done < n) {
        let started = Instant::now();
        let now = monotonic_seconds();
        let d = svc.tick(now)?;
        done += 1;
        if svc.ready() && !announced {
            sd_notify("READY=1"); // only once control has actually been applied
            announced = true;
        }
        if verbose {
            let t = svc.last_temps();
            let fmt = |v: Option<f64>| v.map_or("?".to_string(), |x| format!("{x:.0}"));
            let ema = d.smoothed_c.map_or("?".to_string(), |x| format!("{x:.1}"));
            let mark = if d.write { "WRITE" } else { "     " };
            println!(
                "{} cpu {} gpu {} ema {}  -> {:.0}% pwm {} {} {}",
                clock(),
                fmt(t.cpu_c),
                fmt(t.gpu_c),
                ema,
                d.duty_pct,
                d.pwm,
                mark,
                d.reason
            );
        }
        sd_notify("WATCHDOG=1");
        if ticks.is_none_or(|n| done < n) {
            // Sleep in slices so a signal is noticed within 100 ms.
            let deadline = started + interval;
            while !STOP.load(Ordering::SeqCst) && Instant::now() < deadline {
                std::thread::sleep((deadline - Instant::now()).min(Duration::from_millis(100)));
            }
        }
    }
    Ok(())
}

fn monotonic_seconds() -> f64 {
    static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

fn clock() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    let t = secs as libc::time_t;
    // SAFETY: localtime_r writes into the provided tm; both pointers are valid.
    unsafe { libc::localtime_r(&t, &mut tm) };
    format!("{:02}:{:02}:{:02}", tm.tm_hour, tm.tm_min, tm.tm_sec)
}

fn safe(args: &Args) -> ExitCode {
    // Must work even when the config is broken: fall back to the compiled-in exit duty.
    let duty = match load(Some(&args.config)) {
        Ok(c) => c.exit_duty_pct,
        Err(e) => {
            eprintln!("fanctl: config error ({e}); using the default exit duty");
            Config::default().exit_duty_pct
        }
    };
    match Pwm::new(&args.sysfs).write(pct_to_pwm(duty)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("fanctl: could not set exit duty: {e}");
            ExitCode::from(1)
        }
    }
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    if args.command == "safe" {
        return safe(&args);
    }
    let mut cfg = match load(Some(&args.config)) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("fanctl: config error: {e}");
            return ExitCode::from(2);
        }
    };
    if let Some(state) = args.state {
        cfg.state_path = state;
    }
    if args.command == "check" {
        return check(&cfg, &args.sysfs);
    }
    let dry_run = args.command == "dry-run";
    let handler: extern "C" fn(libc::c_int) = on_signal;
    // SAFETY: installing a signal handler that only stores to an atomic.
    unsafe {
        libc::signal(libc::SIGTERM, handler as usize);
        libc::signal(libc::SIGINT, handler as usize);
    }
    let verbose = dry_run || std::env::var_os("FANCTL_VERBOSE").is_some();
    let mut svc = Service::new(cfg, args.sysfs, Box::new(NvmlGpu::default()), dry_run);
    let result = run_loop(&mut svc, args.ticks, verbose);
    // Every exit path leaves the fan at a safe duty. (A crash aborts instead; the unit's
    // ExecStopPost=fanctl safe covers that case.)
    sd_notify("STOPPING=1");
    svc.shutdown();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("fanctl: {e}");
            ExitCode::from(1)
        }
    }
}
