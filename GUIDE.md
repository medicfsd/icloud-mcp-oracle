# Self-host iCloud MCP on Oracle Always Free

This package runs a [Model Context Protocol](https://modelcontextprotocol.io) server for **your** iCloud Mail, Calendar, Contacts, and Reminders. It listens only on the server itself. Caddy terminates HTTPS and proxies `/mcp/icloud`. You add it to Grok Bot as a custom connector with a static `Authorization: Bearer` header. There is no web UI and no login page on this host.

One Apple **app-specific password** covers IMAP, SMTP, CalDAV, and CardDAV. It is not your Apple ID password. Times in tool results use the `ICLOUD_TZ` zone (default `America/Phoenix`, labeled AZ). Set `ICLOUD_TZ` to your IANA time zone if you live elsewhere. Timed calendar values are stored in UTC so daylight saving stays correct.

## What you need

- An Oracle Cloud account with Always Free capacity
- A free [DuckDNS](https://www.duckdns.org) subdomain (or any DNS name you control)
- An Apple ID with two-factor authentication (required for app-specific passwords)
- An SSH key pair
- About 20 minutes

## 1. Create the Oracle instance

1. In the Oracle Cloud console, create a **Compute** instance.
2. Image: **Oracle Linux 9**.
3. Shape: an Always Free shape (Ampere A1 flex or VM.Standard.E2.1.Micro).
4. Add your SSH public key. Note the `opc` user.
5. Copy the instance **public IP**.
6. Open the subnet **security list** (or NSG) and add ingress for **TCP 80** and **TCP 443** from `0.0.0.0/0`. Port 22 is usually already open.

## 2. Point a name at the IP

1. Create a subdomain at DuckDNS, for example `your-name.duckdns.org`.
2. Set its IP to the instance public IP.
3. Wait a minute, then check: `dig +short your-name.duckdns.org`.

## 3. Get the code onto the server

```bash
ssh opc@YOUR_PUBLIC_IP
sudo dnf install -y git
sudo git clone https://github.com/medicfsd/icloud-mcp-oracle.git /opt/icloud-mcp
```

## 4. Run setup

```bash
sudo bash /opt/icloud-mcp/setup.sh your-name.duckdns.org
```

That script:

- Opens firewalld for HTTP and HTTPS
- Installs Caddy from the `@caddy/caddy` COPR into `/etc/caddy/Caddyfile`
- Installs Python 3.11, a venv, the `icloudmcp` system user, and the systemd unit
- Enables SELinux `httpd_can_network_connect` so Caddy can proxy to `127.0.0.1:8092`
- Enables and starts Caddy (certificate is automatic)
- Does **not** create secrets or start the MCP service until the env file exists

## 5. Create secrets (on the server only)

### Apple app-specific password

1. Sign in at <https://account.apple.com>.
2. **Sign-In and Security** → **App-Specific Passwords** → generate one.
3. Name it something like `iCloud MCP`. Apple shows the password once.

### Bearer token

On the server:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the output somewhere safe. Do not put the app password or the token in chat, email, tickets, or git.

### Env file

```bash
sudo install -m 600 -o root -g root /dev/null /etc/icloud-mcp.env
sudoedit /etc/icloud-mcp.env
```

Paste values like `env.example` (placeholders only in the package):

```
ICLOUD_USER=you@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
MCP_BEARER_TOKEN=the-long-random-token
MCP_PUBLIC_HOST=your-name.duckdns.org
# Optional. IANA zone for shown times and calendar writes (default America/Phoenix).
# ICLOUD_TZ=America/New_York
```

Then:

```bash
sudo systemctl restart icloud-mcp
sudo systemctl --no-pager status icloud-mcp
```

systemd reads the env file as root before dropping to user `icloudmcp`.

## 6. Smoke test

```bash
sudo /opt/icloud-mcp/venv/bin/python /opt/icloud-mcp/smoke_test.py
```

This logs into IMAP and SMTP (no message is sent), lists calendar and reminder-list names, counts address books, and calls MCP `initialize` on localhost. It does not print subjects, contact names, the password, or the token.

## 7. Connect in Grok Bot

Add a **custom remote MCP** connector:

| Field | Value |
| --- | --- |
| Name | `icloud` (or any label you like) |
| URL | `https://your-name.duckdns.org/mcp/icloud` |
| Header name | `Authorization` |
| Header value | `Bearer <your-token>` (the word Bearer, one space, then the token) |
| OAuth client id / secret | leave **empty** |

This server does not implement OAuth. A 401 is a plain Bearer challenge. Transport is Streamable HTTP with a single JSON body per response.

## 8. First safe calls

Ask the bot to list tools, then try:

- `calendar_list_calendars`
- `mail_list_mailboxes`

Do **not** test with `mail_send_message`, `mail_reply_to_message`, or `calendar_delete_event`. Those change real data or send real email.

## Troubleshooting

**Service not running**

```bash
sudo journalctl -u icloud-mcp -n 80 --no-pager
sudo systemctl status icloud-mcp
```

**Caddy / HTTPS**

```bash
sudo journalctl -u caddy -n 80 --no-pager
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

**SELinux blocks the proxy or outbound iCloud**

```bash
sudo ausearch -m avc -ts recent
sudo setsebool -P httpd_can_network_connect 1
```

**Auth check from outside (expect 401 without a token)**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://your-name.duckdns.org/mcp/icloud
```

You should see `401`. Anything else means the route or TLS setup needs attention.

**Wrong host header**

`MCP_PUBLIC_HOST` must match the public hostname. The process also accepts `127.0.0.1` and `localhost` for local smoke tests.

## What it can and cannot do

| App | Protocol | Tools |
| --- | --- | --- |
| Mail | IMAP `imap.mail.me.com:993`, SMTP `smtp.mail.me.com:587` | `mail_*` |
| Calendar | CalDAV `caldav.icloud.com` | `calendar_*` |
| Contacts | CardDAV `contacts.icloud.com` | `contacts_*` |
| Reminders | CalDAV VTODO (what Apple still exposes) | `reminders_*` |

**Not available** with an app-specific password (and not on this server):

- **Notes** (CloudKit)
- **iCloud Drive**
- **Photos**
- **Find My**

**Reminders caveat.** Apple moved many Reminders lists to CloudKit. Upgraded lists often do not appear here, or only Apple's placeholder text comes back. `reminders_list` hides those placeholders and sets a `warning`. Lists that were never upgraded still work.

## Security notes

- Never paste the app-specific password or the bearer token into chat.
- Keep `/etc/icloud-mcp.env` mode `600`, owned by root. Do not commit a filled-in copy.
- Rotate by revoking the app-specific password at account.apple.com and generating a new bearer; update the env file and `sudo systemctl restart icloud-mcp`.
- The MCP process binds to `127.0.0.1` only. Caddy is the only public door. Every other path returns 404.
- The unit does not log Authorization headers, message bodies, or the password.

## Layout of this package

| Path | Role |
| --- | --- |
| `GUIDE.md` | This guide |
| `setup.sh` | Root installer for Oracle Linux 9 (MCP-only host) |
| `remote-install.sh` | Python venv, user, systemd unit (called by setup.sh) |
| `Caddyfile` | Standalone site for `{$MCP_HOST}` / your DuckDNS name |
| `caddy-route.caddy` | The `/mcp/icloud` route snippet alone |
| `icloud-mcp.service` | systemd unit |
| `env.example` | Placeholder env vars |
| `icloud_mcp/` | Application code |
| `smoke_test.py` | Non-destructive login and local MCP check |
| `tests/` | Offline unit tests (no network) |

## Offline tests (optional)

On any machine with Python 3.11+:

```bash
cd /path/to/icloud-mcp-share
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```
