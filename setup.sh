#!/bin/bash
# Install the iCloud MCP connector on a fresh Oracle Linux 9 host (MCP only).
# Run as root after this folder has been copied to /opt/icloud-mcp:
#
#   sudo bash /opt/icloud-mcp/setup.sh your-name.duckdns.org
#
# Never creates or prints secrets. You create /etc/icloud-mcp.env yourself.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash /opt/icloud-mcp/setup.sh <hostname>" >&2
  exit 1
fi

HOST="${1:-}"
if [[ -z "$HOST" ]]; then
  echo "Usage: sudo bash /opt/icloud-mcp/setup.sh your-name.duckdns.org" >&2
  exit 1
fi
if [[ "$HOST" == *"/"* || "$HOST" == *":"* || "$HOST" == *" "* ]]; then
  echo "Hostname must be a bare DNS name (no scheme, port, or spaces)." >&2
  exit 1
fi

ROOT=/opt/icloud-mcp
if [[ ! -d "$ROOT/icloud_mcp" || ! -f "$ROOT/requirements.txt" ]]; then
  echo "Expected connector files under $ROOT. Copy the share folder there first." >&2
  exit 1
fi

echo "==> Opening firewall for HTTP/HTTPS"
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-service=http --add-service=https
  firewall-cmd --reload
else
  echo "firewall-cmd not found; open TCP 80 and 443 in the Oracle security list and locally if needed."
fi

echo "==> Installing Caddy from COPR @caddy/caddy"
if ! command -v caddy >/dev/null 2>&1; then
  dnf install -y 'dnf-command(copr)'
  dnf copr enable -y @caddy/caddy
  dnf install -y caddy
fi

echo "==> Installing Python 3.11 venv, service user, and systemd unit"
bash "$ROOT/remote-install.sh"

echo "==> Installing Caddyfile for $HOST"
# Substitute a concrete site address. Caddy also accepts {$MCP_HOST} when that
# env var is set for the caddy service; writing the hostname keeps the default
# systemd unit simple.
TMP="$(mktemp)"
sed "s/{\$MCP_HOST}/$HOST/g" "$ROOT/Caddyfile" > "$TMP"
install -o root -g root -m 644 "$TMP" /etc/caddy/Caddyfile
rm -f "$TMP"

# SELinux: allow Caddy (httpd_t) to proxy to the local MCP listener.
if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" != "Disabled" ]]; then
  echo "==> Enabling SELinux httpd_can_network_connect for Caddy reverse_proxy"
  setsebool -P httpd_can_network_connect 1 || true
  restorecon -R /opt/icloud-mcp /etc/caddy /etc/systemd/system/icloud-mcp.service >/dev/null 2>&1 || true
fi

systemctl enable --now caddy
systemctl reload caddy 2>/dev/null || systemctl restart caddy

if [[ ! -f /etc/icloud-mcp.env ]]; then
  echo
  echo "icloud-mcp is installed but not started yet."
  echo "Create /etc/icloud-mcp.env as root, mode 600 (see env.example), then:"
  echo "  sudo systemctl restart icloud-mcp"
  echo "Do not paste the Apple app-specific password or the bearer token into chat."
else
  systemctl restart icloud-mcp.service
  systemctl --no-pager --lines=20 status icloud-mcp.service || true
fi

echo
echo "Done. Public URL: https://$HOST/mcp/icloud"
echo "Smoke test (after the env file exists):"
echo "  sudo $ROOT/venv/bin/python $ROOT/smoke_test.py"
