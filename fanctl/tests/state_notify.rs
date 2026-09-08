use fanctl::notify::sd_notify_to;
use fanctl::state::write_state;
use std::os::unix::net::UnixDatagram;

#[test]
fn sd_notify_sends_datagram_and_is_quiet_without_socket() {
    let dir = tempfile::tempdir().unwrap();
    let sock_path = dir.path().join("notify");
    let server = UnixDatagram::bind(&sock_path).unwrap();
    server
        .set_read_timeout(Some(std::time::Duration::from_secs(1)))
        .unwrap();
    assert!(sd_notify_to(
        Some(sock_path.to_str().unwrap()),
        "WATCHDOG=1"
    ));
    let mut buf = [0u8; 64];
    let n = server.recv(&mut buf).unwrap();
    assert_eq!(&buf[..n], b"WATCHDOG=1");
    assert!(!sd_notify_to(None, "READY=1"));
    assert!(!sd_notify_to(
        Some(dir.path().join("missing").to_str().unwrap()),
        "READY=1"
    ));
}

#[test]
fn state_is_written_atomically_and_never_panics() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("state.json");
    write_state(&p, &serde_json::json!({"duty_pct": 25, "reason": "steady"}));
    let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
    assert_eq!(v["duty_pct"], 25);
    assert!(std::fs::read_dir(dir.path()).unwrap().all(|e| !e
        .unwrap()
        .file_name()
        .to_string_lossy()
        .ends_with(".tmp")));
    write_state(
        &dir.path().join("no/such/dir/state.json"),
        &serde_json::json!({"x": 1}),
    );
}
