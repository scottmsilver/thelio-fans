#!/usr/bin/env bash
# Build the release binary as an ordinary user. Run before install.sh. No root needed.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here/../fanctl"
cargo test -q
cargo build --release -q
ls -l target/release/fanctl
echo "now: sudo $here/install.sh"
