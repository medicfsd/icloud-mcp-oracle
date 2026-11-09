# iCloud MCP for Oracle Cloud Always Free

Self-hosted [MCP](https://modelcontextprotocol.io) server for iCloud Mail, Calendar, Contacts, and Reminders. Built for a fresh Oracle Cloud Always Free host running **MCP only** (Caddy + this service; no web UI).

**Start here:** [GUIDE.md](GUIDE.md)

Quick path: on Oracle Linux 9, `sudo git clone https://github.com/medicfsd/icloud-mcp-oracle.git /opt/icloud-mcp`, run `sudo bash setup.sh your-name.duckdns.org`, create `/etc/icloud-mcp.env` from `env.example` (never paste secrets into chat), smoke-test, then add `https://your-name.duckdns.org/mcp/icloud` to Grok Bot with header `Authorization: Bearer <token>` and empty OAuth fields.

Optional: set `ICLOUD_TZ` (IANA name, default `America/Phoenix`) in `/etc/icloud-mcp.env` so tool times and calendar writes match your local zone.
