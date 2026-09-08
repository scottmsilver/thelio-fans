//! fanctl: hold the Thelio Mira intake fan at a smooth, never-stopping duty.
//!
//! Runs as an unprivileged systemd service whose only writable hardware attribute is the
//! Io board's `pwm2`. Design: docs/superpowers/specs/2026-09-08-fanctl-design.md.
pub mod config;
pub mod controller;
pub mod notify;
pub mod pwm;
pub mod sensors;
pub mod service;
pub mod state;
