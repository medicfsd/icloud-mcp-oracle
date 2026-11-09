#!/bin/bash
# Run on the Oracle server as root, after the files are in /opt/icloud-mcp.
# setup.sh (or a manual copy) places files here and runs this. It does not create or print the env file.
set -euo pipefail

if ! command -v python3.11 >/dev/null 2>&1; then
  dnf install -y python3.11
fi

if ! python3.11 -m venv /opt/icloud-mcp/venv; then
  dnf install -y python3.11-pip
  python3.11 -m venv /opt/icloud-mcp/venv
fi

/opt/icloud-mcp/venv/bin/pip install --disable-pip-version-check --upgrade pip
/opt/icloud-mcp/venv/bin/pip install --disable-pip-version-check -r /opt/icloud-mcp/requirements.txt

if ! id icloudmcp >/dev/null 2>&1; then
  nologin=/usr/sbin/nologin
  [[ -x "$nologin" ]] || nologin=/sbin/nologin
  useradd --system --home-dir /nonexistent --shell "$nologin" icloudmcp
fi

install -o root -g root -m 644 /opt/icloud-mcp/icloud-mcp.service /etc/systemd/system/icloud-mcp.service
restorecon -R /opt/icloud-mcp /etc/systemd/system/icloud-mcp.service >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable icloud-mcp.service

if [[ -f /etc/icloud-mcp.env ]]; then
  systemctl restart icloud-mcp.service
  systemctl --no-pager --lines=20 status icloud-mcp.service || true
else
  echo "icloud-mcp is installed but not started: create /etc/icloud-mcp.env first (root, mode 600)."
fi
