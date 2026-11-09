"""Local wall time for tool results and calendar display.

Zone comes from ICLOUD_TZ (IANA name). Default is America/Phoenix. The process
reads the env once at import; systemd passes values in before start. A bad
ICLOUD_TZ raises ConfigError (no silent fallback). Timed calendar values are
written as UTC (Z); this module is for local display and parsing only.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icloud_mcp.errors import ConfigError, ServiceError

DEFAULT_TZ_NAME = "America/Phoenix"


def parse_icloud_tz(raw: str | None = None) -> tuple[str, ZoneInfo]:
    """Resolve an IANA zone name. Empty/None uses ICLOUD_TZ or the default.

    Raises ConfigError when the name is not a known zone.
    """
    if raw is None:
        text = os.environ.get("ICLOUD_TZ", "").strip()
    else:
        text = raw.strip()
    name = text or DEFAULT_TZ_NAME
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"ICLOUD_TZ={name!r} is not a known IANA time zone. "
            "Use a name like America/Phoenix or America/New_York."
        ) from exc
    except Exception as exc:
        raise ConfigError(f"ICLOUD_TZ={name!r} is not usable.") from exc


def tz_name() -> str:
    """Configured IANA zone name (ICLOUD_TZ, default America/Phoenix)."""
    return TZ_NAME


def tz_label() -> str:
    """Short label for instructions. AZ for Phoenix; EST/EDT-style when the zone has DST."""
    if TZ_NAME == DEFAULT_TZ_NAME:
        return "AZ"
    winter = datetime(2026, 1, 15, 12, tzinfo=AZ).strftime("%Z")
    summer = datetime(2026, 7, 15, 12, tzinfo=AZ).strftime("%Z")
    if winter and summer and winter != summer:
        return f"{winter}/{summer}"
    return winter or summer or TZ_NAME.rsplit("/", 1)[-1]


def _label_for(moment: datetime) -> str:
    if TZ_NAME == DEFAULT_TZ_NAME:
        return "AZ"
    abbr = moment.strftime("%Z")
    return abbr or TZ_NAME.rsplit("/", 1)[-1]


TZ_NAME, AZ = parse_icloud_tz()


def show_az(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=AZ)
    local = moment.astimezone(AZ)
    return local.strftime("%Y-%m-%d %H:%M") + " " + _label_for(local)


def show_all_day(day: date) -> str:
    return day.isoformat() + " (all day) " + tz_label()


def parse_az_bound(value: str, *, end: bool) -> datetime:
    """Parse a calendar bound. Date-only end is the next local midnight (exclusive).

    A datetime end is treated as inclusive of that minute, so the returned instant
    is one minute later. CalDAV time ranges are exclusive at the end.
    """
    text = value.strip()
    hint = f"Use YYYY-MM-DD or YYYY-MM-DDTHH:MM, {TZ_NAME}."
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise ServiceError(hint) from exc
        start = datetime(day.year, day.month, day.day, tzinfo=AZ)
        return start + timedelta(days=1) if end else start
    if len(text) == 16 and text[10] == "T":
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M").replace(tzinfo=AZ)
        except ValueError as exc:
            raise ServiceError(hint) from exc
        return parsed + timedelta(minutes=1) if end else parsed
    raise ServiceError(hint)


def parse_mail_day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ServiceError("Mail dates must be YYYY-MM-DD.") from exc


def imap_day(day: date) -> str:
    return day.strftime("%d-%b-%Y")


def to_caldav_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def check_range(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ServiceError("The end must be after the start.")
    if end - start > timedelta(days=400):
        raise ServiceError("The date range is too long. Keep it to 400 days or less.")
