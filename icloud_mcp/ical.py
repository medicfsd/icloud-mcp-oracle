"""A small iCalendar reader and writer for the VEVENT and VTODO fields we use."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from icloud_mcp.dates import AZ


@dataclass
class Prop:
    name: str
    params: dict[str, str]
    value: str


@dataclass
class Component:
    name: str
    props: list[Prop] = field(default_factory=list)
    children: list["Component"] = field(default_factory=list)

    def first(self, name: str) -> Prop | None:
        for prop in self.props:
            if prop.name == name:
                return prop
        return None

    def text(self, name: str, default: str = "") -> str:
        prop = self.first(name)
        return prop.value if prop else default

    def set(self, name: str, value: str, params: dict[str, str] | None = None) -> None:
        params = dict(params or {})
        for prop in self.props:
            if prop.name == name:
                prop.value = value
                prop.params = params
                return
        self.props.append(Prop(name, params, value))

    def remove(self, name: str) -> None:
        self.props = [prop for prop in self.props if prop.name != name]


def unfold(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in normalized.split("\n"):
        if not line:
            continue
        if line[0] in " \t" and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def unescape_text(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append({"n": "\n", "N": "\n", "\\": "\\", ",": ",", ";": ";"}.get(nxt, nxt))
            index += 2
        else:
            out.append(value[index])
            index += 1
    return "".join(out)


def _split_unquoted(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
            current.append(char)
        elif char == separator and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def parse_content_line(line: str) -> tuple[str, dict[str, str], str]:
    quoted = False
    split_at = None
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            split_at = index
            break
    if split_at is None:
        raise ValueError("calendar line has no value")
    left, raw_value = line[:split_at], line[split_at + 1 :]
    bits = _split_unquoted(left, ";")
    name = bits[0].strip().upper()
    params: dict[str, str] = {}
    for bit in bits[1:]:
        if "=" not in bit:
            continue
        key, val = bit.split("=", 1)
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        params[key.strip().upper()] = val
    return name, params, unescape_text(raw_value)


def parse_ics(text: str) -> Component:
    root: Component | None = None
    stack: list[Component] = []
    for line in unfold(text):
        if line.startswith("BEGIN:"):
            component = Component(line[6:].strip().upper())
            if stack:
                stack[-1].children.append(component)
            else:
                root = component
            stack.append(component)
            continue
        if line.startswith("END:"):
            if not stack:
                raise ValueError("unexpected END")
            stack.pop()
            continue
        if not stack:
            continue
        name, params, value = parse_content_line(line)
        stack[-1].props.append(Prop(name, params, value))
    if root is None or stack:
        raise ValueError("incomplete calendar object")
    return root


def fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    buffer = bytearray()
    limit = 75
    for char in line:
        encoded = char.encode("utf-8")
        if len(buffer) + len(encoded) > limit and buffer:
            parts.append(buffer.decode("utf-8"))
            buffer = bytearray(b" ")
            limit = 75
        buffer.extend(encoded)
    if buffer:
        parts.append(buffer.decode("utf-8"))
    return "\r\n".join(parts)


def format_prop(prop: Prop) -> str:
    pieces = [prop.name]
    for key, val in prop.params.items():
        if any(char in val for char in ';:"'):
            pieces.append(f'{key}="{val}"')
        else:
            pieces.append(f"{key}={val}")
    return fold(";".join(pieces) + ":" + escape_text(prop.value))


def serialize(component: Component) -> str:
    lines = [f"BEGIN:{component.name}"]
    for prop in component.props:
        lines.append(format_prop(prop))
    for child in component.children:
        lines.append(serialize(child).rstrip("\r\n"))
    lines.append(f"END:{component.name}")
    return "\r\n".join(lines) + "\r\n"


def parse_when(prop: Prop) -> tuple[datetime | date, bool]:
    """Read one iCalendar date-time.

    DATE (VALUE=DATE, or eight digits and no T) is an all-day date.
    A trailing Z is UTC: the stamp is the first 15 characters (the Z is not
    part of the clock time), then the instant is converted to the local zone.
    TZID uses that zone. A floating time has no zone and is read as local.
    """
    value = prop.value.strip()
    if prop.params.get("VALUE", "").upper() == "DATE" or ("T" not in value and len(value) == 8):
        return datetime.strptime(value[:8], "%Y%m%d").date(), True
    if value.upper().endswith("Z"):
        parsed = datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return parsed.astimezone(AZ), False
    naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    tzid = prop.params.get("TZID")
    if tzid:
        from zoneinfo import ZoneInfo

        try:
            return naive.replace(tzinfo=ZoneInfo(tzid)), False
        except Exception:
            return naive.replace(tzinfo=AZ), False
    return naive.replace(tzinfo=AZ), False


def write_when(value: datetime | date, all_day: bool) -> tuple[str, dict[str, str]]:
    """Write a date or date-time for CalDAV.

    All-day stays as VALUE=DATE in the local zone's calendar day.
    Timed values are always UTC with a trailing Z so DST zones need no VTIMEZONE.
    Display and parse_when still convert to ICLOUD_TZ local time.
    """
    if all_day:
        day = value.astimezone(AZ).date() if isinstance(value, datetime) else value
        return day.strftime("%Y%m%d"), {"VALUE": "DATE"}
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=AZ)
    else:
        moment = datetime(value.year, value.month, value.day, tzinfo=AZ)
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ"), {}


def show_when(value: datetime | date, all_day: bool) -> str:
    from icloud_mcp.dates import show_all_day, show_az

    if all_day:
        day = value if isinstance(value, date) and not isinstance(value, datetime) else value.astimezone(AZ).date()
        return show_all_day(day)
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day, tzinfo=AZ)
    return show_az(value)


def display_end(start: datetime | date, end: datetime | date | None, all_day: bool) -> str:
    if end is None:
        return ""
    if all_day and isinstance(end, date) and not isinstance(end, datetime):
        inclusive = end - timedelta(days=1)
        start_day = start if isinstance(start, date) and not isinstance(start, datetime) else start.astimezone(AZ).date()
        if inclusive < start_day:
            inclusive = start_day
        return show_when(inclusive, True)
    return show_when(end, False)


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _as_aware(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=AZ)
    return datetime(value.year, value.month, value.day, tzinfo=AZ)


def _span(component: Component) -> tuple[datetime | date, datetime | date, bool] | None:
    start_prop = component.first("DTSTART")
    if start_prop is None:
        return None
    try:
        start, all_day = parse_when(start_prop)
    except ValueError:
        return None
    end_prop = component.first("DTEND")
    if end_prop is not None:
        try:
            end, _end_all_day = parse_when(end_prop)
        except ValueError:
            end = start
    elif all_day and not isinstance(start, datetime):
        end = start + timedelta(days=1)
    elif isinstance(start, datetime):
        end = start + timedelta(hours=1)
    else:
        end = start
    return start, end, all_day


def _overlaps(start: datetime | date, end: datetime | date, window_start: datetime, window_end: datetime) -> bool:
    return _as_aware(start) < window_end and _as_aware(end) > window_start


def _rule_parts(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for piece in text.split(";"):
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        found[key.strip().upper()] = value.strip()
    return found


def _when_limit(text: str) -> datetime | None:
    if not text:
        return None
    try:
        value, _all_day = parse_when(Prop("UNTIL", {}, text))
    except ValueError:
        return None
    return _as_aware(value)


def _stamp_key(value: datetime | date, all_day: bool) -> str:
    if all_day or not isinstance(value, datetime):
        day = value if not isinstance(value, datetime) else value.astimezone(AZ).date()
        return "d:" + day.isoformat()
    return "t:" + value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _excluded(start: datetime | date, all_day: bool, skipped: set[str]) -> bool:
    if _stamp_key(start, all_day) in skipped:
        return True
    if not all_day and isinstance(start, datetime):
        if ("d:" + start.astimezone(AZ).date().isoformat()) in skipped:
            return True
    return False


def _exception_keys(component: Component) -> set[str]:
    skipped: set[str] = set()
    for prop in component.props:
        if prop.name != "EXDATE":
            continue
        all_day = prop.params.get("VALUE", "").upper() == "DATE"
        for piece in prop.value.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                value, piece_all_day = parse_when(Prop("EXDATE", prop.params, piece))
            except ValueError:
                continue
            skipped.add(_stamp_key(value, all_day or piece_all_day))
    return skipped


def _weekday_numbers(text: str) -> list[int]:
    days: list[int] = []
    for piece in text.split(","):
        name = piece.strip()[-2:].upper()
        if name in _WEEKDAYS:
            days.append(_WEEKDAYS[name])
    return days


def _combine(day: date, original: datetime | date) -> datetime | date:
    if isinstance(original, datetime):
        aware = _as_aware(original)
        local = aware.astimezone(AZ)
        return datetime(day.year, day.month, day.day, local.hour, local.minute, local.second, tzinfo=AZ)
    return day


def _keep(moment: datetime | date, start: datetime | date, until: datetime | None) -> bool:
    aware = _as_aware(moment)
    if aware < _as_aware(start):
        return False
    if until is not None and aware > until:
        return False
    return True


def _occurrence_starts(start: datetime | date, rule: dict[str, str], window_end: datetime) -> list[datetime | date]:
    freq = rule.get("FREQ", "").upper()
    try:
        interval = max(1, int(rule.get("INTERVAL", "1") or "1"))
    except ValueError:
        interval = 1
    try:
        count = int(rule["COUNT"]) if rule.get("COUNT") else None
    except ValueError:
        count = None
    until = _when_limit(rule.get("UNTIL", ""))
    byday = _weekday_numbers(rule.get("BYDAY", ""))
    origin = start.date() if isinstance(start, datetime) else start
    produced: list[datetime | date] = []
    horizon = window_end.astimezone(AZ).date()

    def add(moment: datetime | date) -> bool:
        if not _keep(moment, start, until):
            return until is not None and _as_aware(moment) > until
        produced.append(moment)
        return count is not None and len(produced) >= count

    if freq == "DAILY":
        allowed = set(byday)
        cursor = origin
        steps = 0
        while steps < 5000 and (count is None or len(produced) < count):
            steps += 1
            if not allowed or cursor.weekday() in allowed:
                if add(_combine(cursor, start)) and count is not None and len(produced) >= count:
                    break
                if count is None and cursor > horizon:
                    break
            cursor += timedelta(days=interval)
        return produced

    if freq == "WEEKLY":
        days = byday or [origin.weekday()]
        week_zero = origin - timedelta(days=origin.weekday())
        week = 0
        while week < 5000 and (count is None or len(produced) < count):
            week_start = week_zero + timedelta(weeks=week * interval)
            if week_start > horizon and (until is None or week_start > until.astimezone(AZ).date()):
                break
            for weekday in sorted(days):
                if add(_combine(week_start + timedelta(days=weekday), start)):
                    return produced
            week += 1
        return produced

    if freq in {"MONTHLY", "YEARLY"}:
        cursor = origin
        steps = 0
        while steps < 5000 and (count is None or len(produced) < count):
            steps += 1
            if add(_combine(cursor, start)):
                break
            if count is None and cursor > horizon:
                break
            if freq == "MONTHLY":
                month_index = cursor.month - 1 + interval
                year = cursor.year + month_index // 12
                month = month_index % 12 + 1
                cursor = date(year, month, min(origin.day, _month_length(year, month)))
            else:
                year = cursor.year + interval
                cursor = date(year, cursor.month, min(origin.day, _month_length(year, cursor.month)))
        return produced

    return [start]


def _month_length(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def _clone_occurrence(master: Component, start: datetime | date, end: datetime | date, all_day: bool) -> Component:
    event = Component("VEVENT")
    for prop in master.props:
        if prop.name in {"DTSTART", "DTEND", "RRULE", "EXDATE", "RECURRENCE-ID"}:
            continue
        event.props.append(Prop(prop.name, dict(prop.params), prop.value))
    start_value, start_params = write_when(start, all_day)
    end_value, end_params = write_when(end, all_day)
    event.set("DTSTART", start_value, start_params)
    event.set("DTEND", end_value, end_params)
    event.set("RECURRENCE-ID", start_value, start_params)
    return event


def _override_for(overrides: list[Component], start: datetime | date, all_day: bool) -> Component | None:
    wanted = _stamp_key(start, all_day)
    for override in overrides:
        prop = override.first("RECURRENCE-ID")
        if prop is None:
            continue
        try:
            value, override_all_day = parse_when(prop)
        except ValueError:
            continue
        if _stamp_key(value, all_day or override_all_day) == wanted:
            return override
    return None


def events_in_window(
    components: list[Component], window_start: datetime, window_end: datetime
) -> list[Component]:
    """Expand repeating events into the requested range. Overrides and EXDATE are honored."""
    grouped: dict[str, list[Component]] = {}
    loose: list[Component] = []
    for component in components:
        if component.name != "VEVENT":
            continue
        uid = component.text("UID")
        if uid:
            grouped.setdefault(uid, []).append(component)
        else:
            loose.append(component)
    chosen: list[Component] = []
    for component in loose:
        span = _span(component)
        if span and _overlaps(span[0], span[1], window_start, window_end):
            chosen.append(component)
    for comps in grouped.values():
        masters = [item for item in comps if item.first("RECURRENCE-ID") is None]
        overrides = [item for item in comps if item.first("RECURRENCE-ID") is not None]
        master = masters[0] if masters else None
        if master is not None and master.text("RRULE"):
            span = _span(master)
            if span is None:
                continue
            start, end, all_day = span
            duration = _as_aware(end) - _as_aware(start)
            skipped = _exception_keys(master)
            rule = _rule_parts(master.text("RRULE"))
            for moment in _occurrence_starts(start, rule, window_end):
                if _excluded(moment, all_day, skipped):
                    continue
                occ_end = _as_aware(moment) + duration
                if not _overlaps(moment, occ_end, window_start, window_end):
                    continue
                override = _override_for(overrides, moment, all_day)
                if override is not None and override.text("STATUS").upper() == "CANCELLED":
                    continue
                chosen.append(override if override is not None else _clone_occurrence(master, moment, occ_end, all_day))
            continue
        for component in comps:
            span = _span(component)
            if span and _overlaps(span[0], span[1], window_start, window_end):
                chosen.append(component)
    return chosen


def find_component(root: Component, kind: str, uid: str) -> Component | None:
    matches = [child for child in root.children if child.name == kind and child.text("UID") == uid]
    masters = [child for child in matches if child.first("RECURRENCE-ID") is None]
    if masters:
        return masters[0]
    return matches[0] if matches else None


def wrap_calendar(*children: Component) -> Component:
    calendar = Component("VCALENDAR")
    calendar.set("VERSION", "2.0")
    calendar.set("PRODID", "-//icloud-mcp//icloud-mcp//EN")
    calendar.set("CALSCALE", "GREGORIAN")
    calendar.children.extend(children)
    return calendar


def utc_stamp(moment: datetime | None = None) -> str:
    current = moment or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
