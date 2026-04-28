#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="$SCRIPT_DIR/systemd"

install -m 0644 "$SYSTEMD_DIR/openclaw-docs-ui.service" /etc/systemd/system/openclaw-docs-ui.service
install -m 0644 "$SYSTEMD_DIR/openclaw-docs-parse-drain.service" /etc/systemd/system/openclaw-docs-parse-drain.service
install -m 0644 "$SYSTEMD_DIR/openclaw-docs-parse-drain.timer" /etc/systemd/system/openclaw-docs-parse-drain.timer

systemctl daemon-reload
systemctl enable --now openclaw-docs-ui.service
systemctl enable --now openclaw-docs-parse-drain.timer

echo "Installed and started:"
systemctl --no-pager --full status openclaw-docs-ui.service || true
systemctl --no-pager --full status openclaw-docs-parse-drain.timer || true
