"""Runtime settings from the environment. The process never reads the env file itself.

systemd reads /etc/icloud-mcp.env as root and passes the values in. Missing or
unusable values raise ConfigError before any network call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from icloud_mcp.dates import parse_icloud_tz
from icloud_mcp.errors import ConfigError

PATH = "/mcp/icloud"
BIND = "127.0.0.1"
DEFAULT_PORT = 8092
IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587
CALDAV_URL = "https://caldav.icloud.com/"
CARDDAV_URL = "https://contacts.icloud.com/"
DEFAULT_PUBLIC_HOST = "your-name.duckdns.org"
DEFAULT_SENT = "Sent Messages"
DEFAULT_TIMEOUT = 30.0
DEFAULT_ATTACHMENT_MAX = 1_000_000
ATTACHMENT_HARD_MAX = 5_000_000


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is missing.")
    if any(ch in value for ch in "\r\n\x00"):
        raise ConfigError(f"{name} cannot contain a newline.")
    return value


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if value < low or value > high:
        raise ConfigError(f"{name} must be between {low} and {high}.")
    return value


@dataclass(frozen=True)
class Config:
    user: str
    credential: str
    bearer: str
    public_host: str
    sent_mailbox: str
    port: int
    timeout: float
    attachment_max: int

    @property
    def secrets(self) -> list[str]:
        return [self.credential, self.bearer]

    @classmethod
    def from_env(cls) -> "Config":
        parse_icloud_tz()  # ICLOUD_TZ must be a real IANA zone (default America/Phoenix)
        user = _required("ICLOUD_USER")
        if user.count("@") != 1 or any(ch.isspace() for ch in user):
            raise ConfigError("ICLOUD_USER must be the iCloud email address.")
        credential = _required("ICLOUD_APP_PASSWORD")
        if len(credential) < 8:
            raise ConfigError("ICLOUD_APP_PASSWORD is too short to be an app-specific password.")
        bearer = _required("MCP_BEARER_TOKEN")
        if len(bearer) < 20 or len(bearer) > 200:
            raise ConfigError("MCP_BEARER_TOKEN must be 20 to 200 characters.")
        if any(ord(ch) < 33 or ord(ch) > 126 for ch in bearer):
            raise ConfigError("MCP_BEARER_TOKEN must be printable ASCII without spaces.")
        public_host = os.environ.get("MCP_PUBLIC_HOST", DEFAULT_PUBLIC_HOST).strip().lower()
        if not public_host or "/" in public_host or ":" in public_host or " " in public_host:
            raise ConfigError("MCP_PUBLIC_HOST must be a hostname, with no scheme or port.")
        sent = os.environ.get("ICLOUD_SENT_MAILBOX", DEFAULT_SENT).strip() or DEFAULT_SENT
        if any(ch in sent for ch in '\r\n\x00"') or len(sent) > 200:
            raise ConfigError("ICLOUD_SENT_MAILBOX is not a usable mailbox name.")
        timeout = _int_env("ICLOUD_TIMEOUT_SECONDS", int(DEFAULT_TIMEOUT), 5, 120)
        attachment_max = _int_env(
            "ICLOUD_ATTACHMENT_MAX_BYTES", DEFAULT_ATTACHMENT_MAX, 1, ATTACHMENT_HARD_MAX
        )
        return cls(
            user=user,
            credential=credential,
            bearer=bearer,
            public_host=public_host,
            sent_mailbox=sent,
            port=_int_env("MCP_PORT", DEFAULT_PORT, 1024, 65535),
            timeout=float(timeout),
            attachment_max=attachment_max,
        )
