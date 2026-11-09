"""Reminders that iCloud still exposes as CalDAV VTODO components.

Upgraded Reminders lists often do not sync. Apple sometimes leaves a list
whose only items are placeholders. Those placeholders are not returned as tasks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from icloud_mcp.dates import parse_az_bound
from icloud_mcp.dav import DavClient, calendar_query_xml, join_child, resolve_by_name
from icloud_mcp.errors import ServiceError
from icloud_mcp.ical import Component, find_component, parse_ics, parse_when, serialize, show_when, utc_stamp, wrap_calendar, write_when

UPGRADED_NOTE = (
    "Apple's CalDAV feed did not return the real items in this list. "
    "Upgraded Reminders lists stay in the Reminders app and often only show a placeholder "
    "over CalDAV, so they are not available with an app-specific password."
)
PLACEHOLDER_TITLES = {
    "where are my reminders?",
    "the creator of this list has upgraded these reminders.",
}
PRIORITIES = {"high": "1", "medium": "5", "low": "9"}


def _lists(dav: DavClient) -> list[dict]:
    return [item for item in dav.list_calendars() if "VTODO" in item["components"]]


def _check_uid(uid: str) -> str:
    text = uid.strip()
    if not text or len(text) > 400 or any(char in text for char in "\r\n\x00<>"):
        raise ServiceError("That reminder uid is not usable.")
    return text


def _check_title(title: str) -> str:
    text = title.strip()
    if not text or len(text) > 500 or any(char in text for char in "\r\n"):
        raise ServiceError("The reminder title is missing, too long, or has a newline.")
    return text


def _is_placeholder(component: Component) -> bool:
    title = component.text("SUMMARY").strip().lower()
    description = component.text("DESCRIPTION").lower()
    return title in PLACEHOLDER_TITLES or "upgraded these reminders" in description


def _is_complete(component: Component) -> bool:
    status = component.text("STATUS").upper()
    return status == "COMPLETED" or bool(component.text("COMPLETED"))


def _public(component: Component, list_name: str) -> dict:
    due = ""
    due_prop = component.first("DUE")
    if due_prop is not None:
        value, all_day = parse_when(due_prop)
        due = show_when(value, all_day)
    priority = component.text("PRIORITY")
    label = {value: key for key, value in PRIORITIES.items()}.get(priority, "")
    return {
        "uid": component.text("UID"),
        "list": list_name,
        "title": component.text("SUMMARY"),
        "notes": component.text("DESCRIPTION")[:4000],
        "due": due,
        "priority": label,
        "completed": _is_complete(component),
    }


def _load(dav: DavClient, list_name: str, uid: str):
    uid = _check_uid(uid)
    collection = resolve_by_name(_lists(dav), list_name, "reminder list")
    items = dav.report(
        collection["href"],
        calendar_query_xml(component="VTODO", start=None, end=None, uid=uid),
        "calendar-data",
    )
    matched = []
    for item in items:
        try:
            root = parse_ics(item["data"])
        except ValueError:
            continue
        component = find_component(root, "VTODO", uid)
        if component is not None:
            matched.append((item, root, component, collection))
    if not matched:
        raise ServiceError(f"No reminder with uid {uid!r} in list {collection['name']!r}.")
    if len(matched) > 1:
        raise ServiceError("That uid matched more than one reminder. Nothing was changed.")
    return matched[0]


def _apply_due(component: Component, due: str) -> None:
    text = due.strip()
    all_day = len(text) == 10
    moment = parse_az_bound(text, end=False)
    token, params = write_when(moment, all_day)
    component.set("DUE", token, params)


class RemindersService:
    def __init__(self, dav: DavClient) -> None:
        self.dav = dav
        self.secrets = dav.secrets

    def list_lists(self) -> dict:
        lists = _lists(self.dav)
        return {
            "lists": [{"name": item["name"]} for item in lists],
            "note": (
                "A list that was upgraded in the Reminders app may be missing here, or "
                "reminders_list may report that only placeholders came back."
            ),
        }

    def list_reminders(self, list_name: str, include_completed: bool = False, limit: int = 50) -> dict:
        collection = resolve_by_name(_lists(self.dav), list_name, "reminder list")
        limit = max(1, min(int(limit), 100))
        items = self.dav.report(
            collection["href"],
            calendar_query_xml(component="VTODO", start=None, end=None, uid=None),
            "calendar-data",
        )
        rows = []
        hidden = 0
        for item in items:
            try:
                root = parse_ics(item["data"])
            except ValueError:
                continue
            for component in root.children:
                if component.name != "VTODO":
                    continue
                if _is_placeholder(component):
                    hidden += 1
                    continue
                if _is_complete(component) and not include_completed:
                    continue
                rows.append(_public(component, collection["name"]))
        warning = UPGRADED_NOTE if hidden else ""
        return {
            "list": collection["name"],
            "truncated": len(rows) > limit,
            "hidden_placeholders": hidden,
            "warning": warning,
            "reminders": rows[:limit],
        }

    def add(
        self,
        list_name: str,
        title: str,
        due: str | None = None,
        notes: str | None = None,
        priority: str | None = None,
    ) -> dict:
        collection = resolve_by_name(_lists(self.dav), list_name, "reminder list")
        title = _check_title(title)
        uid = str(uuid.uuid4())
        todo = Component("VTODO")
        todo.set("UID", uid)
        todo.set("DTSTAMP", utc_stamp())
        todo.set("SUMMARY", title)
        todo.set("STATUS", "NEEDS-ACTION")
        todo.set("PERCENT-COMPLETE", "0")
        if due:
            _apply_due(todo, due)
        if notes:
            if len(notes) > 4000:
                raise ServiceError("The reminder notes are too long.")
            todo.set("DESCRIPTION", notes.replace("\x00", ""))
        if priority:
            if priority.lower() not in PRIORITIES:
                raise ServiceError("Priority must be high, medium, or low.")
            todo.set("PRIORITY", PRIORITIES[priority.lower()])
        payload = serialize(wrap_calendar(todo)).encode("utf-8")
        self.dav.put(join_child(collection["href"], uid + ".ics"), payload, "text/calendar; charset=utf-8", create=True)
        created = _public(todo, collection["name"])
        created["created"] = True
        return created

    def complete(self, list_name: str, uid: str) -> dict:
        item, root, component, collection = _load(self.dav, list_name, uid)
        if _is_placeholder(component):
            raise ServiceError(UPGRADED_NOTE)
        component.set("STATUS", "COMPLETED")
        component.set("PERCENT-COMPLETE", "100")
        component.set("COMPLETED", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        component.set("DTSTAMP", utc_stamp())
        self.dav.put(item["href"], serialize(root).encode("utf-8"), "text/calendar; charset=utf-8", etag=item["etag"])
        done = _public(component, collection["name"])
        done["completed"] = True
        return done

    def edit(
        self,
        list_name: str,
        uid: str,
        title: str | None = None,
        due: str | None = None,
        notes: str | None = None,
        priority: str | None = None,
    ) -> dict:
        if all(value is None for value in (title, due, notes, priority)):
            raise ServiceError("Pass at least one field to change.")
        item, root, component, collection = _load(self.dav, list_name, uid)
        if _is_placeholder(component):
            raise ServiceError(UPGRADED_NOTE)
        if title is not None:
            component.set("SUMMARY", _check_title(title))
        if due is not None:
            _apply_due(component, due)
        if notes is not None:
            if len(notes) > 4000:
                raise ServiceError("The reminder notes are too long.")
            component.set("DESCRIPTION", notes.replace("\x00", ""))
        if priority is not None:
            if priority.lower() not in PRIORITIES:
                raise ServiceError("Priority must be high, medium, or low.")
            component.set("PRIORITY", PRIORITIES[priority.lower()])
        component.set("DTSTAMP", utc_stamp())
        self.dav.put(item["href"], serialize(root).encode("utf-8"), "text/calendar; charset=utf-8", etag=item["etag"])
        edited = _public(component, collection["name"])
        edited["updated"] = True
        return edited
