//! fanctl: hold the Thelio Mira intake fan at a smooth, never-stopping duty.
//!
//! Runs as an unprivileged systemd service whose only writable hardware attribute is the
//! Io board's `pwm2`. Design: docs/superpowers/specs/2026-09-08-fanctl-design.md.
//!
//! Module map: [`config`] settings and validation; [`controller`] the pure control law;
//! [`sensors`] temperature inputs; [`pwm`] the one writable file; [`service`] one tick
//! tying those together and writing the state file; [`state`] and [`notify`] the JSON
//! status file and `sd_notify`. The binary in `main.rs` adds signals, sleeping and the CLI.
#![warn(missing_docs)]
pub mod config;
pub mod controller;
pub mod notify;
pub mod pwm;
pub mod sensors;
pub mod service;
pub mod state;
