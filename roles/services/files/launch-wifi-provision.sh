#!/usr/bin/env bash
# Openbox recovery shortcut: elevate only the fixed recovery entrypoint.
set -euo pipefail
exec /usr/bin/sudo -n /usr/local/sbin/wifi-provision-recovery
