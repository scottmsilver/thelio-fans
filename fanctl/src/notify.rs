//! Minimal sd_notify: one datagram to $NOTIFY_SOCKET. No dependency on libsystemd.

use std::os::linux::net::SocketAddrExt;
use std::os::unix::net::{SocketAddr, UnixDatagram};

/// Send one notification to the socket named by NOTIFY_SOCKET. False when absent or failing.
pub fn sd_notify(message: &str) -> bool {
    let target = std::env::var("NOTIFY_SOCKET").ok();
    sd_notify_to(target.as_deref(), message)
}

/// Same as [`sd_notify`] with an explicit socket path (`@name` for an abstract socket).
pub fn sd_notify_to(socket_path: Option<&str>, message: &str) -> bool {
    let Some(target) = socket_path.filter(|s| !s.is_empty()) else {
        return false;
    };
    let Ok(sock) = UnixDatagram::unbound() else {
        return false;
    };
    let sent = if let Some(name) = target.strip_prefix('@') {
        SocketAddr::from_abstract_name(name.as_bytes())
            .and_then(|addr| sock.send_to_addr(message.as_bytes(), &addr))
    } else {
        sock.send_to(message.as_bytes(), target)
    };
    sent.is_ok()
}
