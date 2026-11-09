"""Calendar operations over CalDAV. Names are the display names iCloud shows."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from icloud_mcp.dates import TZ_NAME, check_range, parse_az_bound, show_az, to_caldav_utc
from icloud_mcp.dav import DavClient, calendar_query_xml, join_child, resolve_by_name
from icloud_mcp.errors import ServiceError
from icloud_mcp.ical import (
    Component,
    events_in_window,
    find_component,
    parse_ics,
    parse_when,
    serialize,
    show_when,
    utc_stamp,
    wrap_calendar,
    write_when,
)

SUMMARY_MAX = 500
DETAIL_MAX = 8_000


def _check_uid(uid: str) -> str:
    text = uid.strip()
    if not text or len(text) > 400 or any(char in text for char in "\r\n\x00<>"):
        raise ServiceError("That event uid is not usable.")
    return text


def _check_summary(summary: str) -> str:
    text = summary.strip()
    if not text or len(text) > SUMMARY_MAX or any(char in text for char in "\r\n"):
        raise ServiceError("The event summary is missing, too long, or has a newline.")
    return text


def _check_detail(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    if len(text) > DETAIL_MAX:
        raise ServiceError(f"The event {label} is too long.")
    return text


def _event_calendars(dav: DavClient) -> list[dict]:
    calendars = [item for item in dav.list_calendars() if "VEVENT" in item["components"] or not item["components"]]
    return calendars


def _require_writable(collection: dict) -> None:
    if collection.get("subscribed") or collection.get("read_only"):
        raise ServiceError(
            f"{collection['name']} is a subscribed read-only calendar. "
            "Events there cannot be created, updated, or deleted."
        )


def _public_calendar(item: dict) -> dict:
    return {
        "name": item["name"],
        "components": item["components"],
        "subscribed": bool(item.get("subscribed")),
        "read_only": bool(item.get("read_only") or item.get("subscribed")),
    }


def _public_event(component: Component, calendar: str) -> dict:
    start_prop = component.first("DTSTART")
    if start_prop is None:
        raise ServiceError("That event has no start time.")
    start, all_day = parse_when(start_prop)
    end_prop = component.first("DTEND")
    end_text = ""
    if end_prop is not None:
        end_value, end_all_day = parse_when(end_prop)
        if all_day or end_all_day:
            end_day = end_value.date() if isinstance(end_value, datetime) else end_value
            start_day = start.date() if isinstance(start, datetime) else start
            inclusive = end_day - timedelta(days=1)
            if inclusive < start_day:
                inclusive = start_day
            end_text = show_when(inclusive, True)
        else:
            end_text = show_when(end_value, False)
    description = component.text("DESCRIPTION")
    return {
        "uid": component.text("UID"),
        "calendar": calendar,
        "summary": component.text("SUMMARY"),
        "start": show_when(start, all_day),
        "end": end_text,
        "all_day": all_day,
        "location": component.text("LOCATION"),
        "description": description[:4000],
        "description_truncated": len(description) > 4000,
        "status": component.text("STATUS"),
        "rrule": component.text("RRULE"),
        "recurrence_id": component.text("RECURRENCE-ID"),
        "_sort": start.isoformat() if isinstance(start, datetime) else start.isoformat(),
    }


def _matches(items: list[dict], uid: str, collection: dict) -> list[tuple]:
    matched = []
    for item in items:
        try:
            root = parse_ics(item["data"])
        except ValueError:
            continue
        component = find_component(root, "VEVENT", uid)
        if component is not None:
            matched.append((item, root, component, collection))
    return matched


def _load(service: "CalendarService", calendar: str, uid: str):
    uid = _check_uid(uid)
    collection = resolve_by_name(_event_calendars(service.dav), calendar, "calendar")
    try:
        items = service.dav.report(
            collection["href"],
            calendar_query_xml(component="VEVENT", start=None, end=None, uid=uid),
            "calendar-data",
        )
    except ServiceError:
        items = []
    matched = _matches(items, uid, collection)
    if not matched and collection.get("subscribed"):
        matched = _matches(
            [{"href": "", "etag": "", "data": service._feed_calendar(collection)}],
            uid,
            collection,
        )
    if not matched:
        raise ServiceError(f"No event with uid {uid!r} in calendar {collection['name']!r}.")
    if len(matched) > 1:
        raise ServiceError("That uid matched more than one event. Nothing was changed.")
    return matched[0]


class CalendarService:
    def __init__(self, dav: DavClient) -> None:
        self.dav = dav
        self.secrets = dav.secrets

    def list_calendars(self) -> dict:
        return {"calendars": [_public_calendar(item) for item in _event_calendars(self.dav)]}

    def _feed_calendar(self, collection: dict) -> str:
        address = collection.get("feed_url") or ""
        if not address:
            raise ServiceError("Could not read the subscribed calendar.")
        payload = self.dav.fetch_feed(address)
        text = payload.decode("utf-8", "replace")
        if "BEGIN:VCALENDAR" not in text.upper():
            raise ServiceError("Could not read the subscribed calendar.")
        return text

    def _read_calendar_data(self, collection: dict, start: str | None, end: str | None, uid: str | None) -> list[dict]:
        try:
            items = self.dav.report(
                collection["href"],
                calendar_query_xml(component="VEVENT", start=start, end=end, uid=uid),
                "calendar-data",
            )
        except ServiceError:
            items = None
        if items:
            return items
        if collection.get("subscribed"):
            return [{"href": "", "etag": "", "data": self._feed_calendar(collection)}]
        if items is None:
            raise ServiceError(f"Could not read calendar {collection['name']!r}.")
        return []

    def list_events(self, calendar: str, start: str, end: str, text: str | None = None, limit: int = 50) -> dict:
        start_at = parse_az_bound(start, end=False)
        end_at = parse_az_bound(end, end=True)
        check_range(start_at, end_at)
        limit = max(1, min(int(limit), 100))
        collection = resolve_by_name(_event_calendars(self.dav), calendar, "calendar")
        items = self._read_calendar_data(
            collection, to_caldav_utc(start_at), to_caldav_utc(end_at), None
        )
        components: list[Component] = []
        for item in items:
            try:
                root = parse_ics(item["data"])
            except ValueError:
                continue
            components.extend(child for child in root.children if child.name == "VEVENT")
        needle = (text or "").strip().lower()
        rows = []
        for component in events_in_window(components, start_at, end_at):
            try:
                public = _public_event(component, collection["name"])
            except ServiceError:
                continue
            haystack = " ".join([public["summary"], public["location"], public["description"]]).lower()
            if needle and needle not in haystack:
                continue
            rows.append(public)
        rows.sort(key=lambda row: row["_sort"])
        for row in rows:
            row.pop("_sort", None)
        return {
            "calendar": collection["name"],
            "start": show_az(start_at),
            "end": show_az(end_at),
            "truncated": len(rows) > limit,
            "events": rows[:limit],
        }

    def get_event(self, calendar: str, uid: str) -> dict:
        _item, _root, component, collection = _load(self, calendar, uid)
        public = _public_event(component, collection["name"])
        public.pop("_sort", None)
        public["repeating"] = bool(public["rrule"])
        return public

    def create_event(
        self,
        calendar: str,
        summary: str,
        start: str,
        end: str | None = None,
        all_day: bool = False,
        description: str | None = None,
        location: str | None = None,
    ) -> dict:
        collection = resolve_by_name(_event_calendars(self.dav), calendar, "calendar")
        _require_writable(collection)
        summary = _check_summary(summary)
        description = _check_detail(description, "description")
        location = _check_detail(location, "location")
        start_at = parse_az_bound(start, end=False)
        if all_day:
            end_at = parse_az_bound(end, end=False) if end else start_at
            end_exclusive = (end_at + timedelta(days=1)) if end else (start_at + timedelta(days=1))
            start_token, start_params = write_when(start_at, True)
            end_token, end_params = write_when(end_exclusive, True)
        else:
            if not end:
                raise ServiceError(f"A timed event needs an end. Use YYYY-MM-DDTHH:MM, {TZ_NAME}.")
            end_at = parse_az_bound(end, end=False)
            if end_at <= start_at:
                raise ServiceError("The end must be after the start.")
            start_token, start_params = write_when(start_at, False)
            end_token, end_params = write_when(end_at, False)
        uid = str(uuid.uuid4())
        event = Component("VEVENT")
        event.set("UID", uid)
        event.set("DTSTAMP", utc_stamp())
        event.set("SUMMARY", summary)
        event.set("DTSTART", start_token, start_params)
        event.set("DTEND", end_token, end_params)
        event.set("STATUS", "CONFIRMED")
        if description:
            event.set("DESCRIPTION", description)
        if location:
            event.set("LOCATION", location)
        payload = serialize(wrap_calendar(event)).encode("utf-8")
        self.dav.put(
            join_child(collection["href"], uid + ".ics"),
            payload,
            "text/calendar; charset=utf-8",
            create=True,
        )
        public = _public_event(event, collection["name"])
        public.pop("_sort", None)
        public["created"] = True
        return public

    def update_event(
        self,
        calendar: str,
        uid: str,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        location: str | None = None,
    ) -> dict:
        if all(value is None for value in (summary, start, end, all_day, description, location)):
            raise ServiceError("Pass at least one field to change.")
        collection = resolve_by_name(_event_calendars(self.dav), calendar, "calendar")
        _require_writable(collection)
        item, root, component, collection = _load(self, calendar, uid)
        if summary is not None:
            component.set("SUMMARY", _check_summary(summary))
        if description is not None:
            component.set("DESCRIPTION", _check_detail(description, "description") or "")
        if location is not None:
            component.set("LOCATION", _check_detail(location, "location") or "")
        if start is not None or end is not None or all_day is not None:
            current_all_day = False
            current_start = component.first("DTSTART")
            if current_start is not None:
                _value, current_all_day = parse_when(current_start)
            use_all_day = current_all_day if all_day is None else all_day
            if start is None or end is None:
                raise ServiceError(f"Changing the time requires both start and end, in {TZ_NAME}.")
            start_at = parse_az_bound(start, end=False)
            end_at = parse_az_bound(end, end=False)
            if use_all_day:
                end_token_source = end_at + timedelta(days=1)
                start_token, start_params = write_when(start_at, True)
                end_token, end_params = write_when(end_token_source, True)
            else:
                if end_at <= start_at:
                    raise ServiceError("The end must be after the start.")
                start_token, start_params = write_when(start_at, False)
                end_token, end_params = write_when(end_at, False)
            component.set("DTSTART", start_token, start_params)
            component.set("DTEND", end_token, end_params)
        component.set("DTSTAMP", utc_stamp())
        payload = serialize(root).encode("utf-8")
        self.dav.put(item["href"], payload, "text/calendar; charset=utf-8", etag=item["etag"])
        public = _public_event(component, collection["name"])
        public.pop("_sort", None)
        public["updated"] = True
        return public

    def delete_event(self, calendar: str, uid: str) -> dict:
        collection = resolve_by_name(_event_calendars(self.dav), calendar, "calendar")
        _require_writable(collection)
        item, _root, component, collection = _load(self, calendar, uid)
        summary = component.text("SUMMARY")
        self.dav.delete(item["href"], item["etag"])
        return {
            "deleted": True,
            "calendar": collection["name"],
            "uid": component.text("UID"),
            "summary": summary,
        }
