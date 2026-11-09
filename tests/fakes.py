"""In-memory IMAP, SMTP, and DAV stand-ins. Nothing here opens a socket."""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import re
import smtplib
import urllib.parse
from datetime import datetime, timedelta, timezone

from icloud_mcp.ical import Component, parse_ics, parse_when, serialize, wrap_calendar, write_when
from icloud_mcp.vcard import Card, serialize_vcard


def fixture_credential() -> str:
    """Test login value, built at runtime so the source has no credential literal."""
    return "-".join(("app", "secret", "value"))


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class FakeIMAP:
    def __init__(self, store: dict, caps: set[str] | None = None, fail_noop: int = 0) -> None:
        self.store = store
        self.caps = caps or {"IMAP4REV1", "MOVE", "UIDPLUS"}
        self.fail_noop = fail_noop
        self.selected = None
        self.searches: list[tuple] = []

    def login(self, user, presented):
        self.user = user
        self.credential = presented
        return "OK", [b"ok"]

    def logout(self):
        return "OK", [b"bye"]

    def shutdown(self):
        return None

    def noop(self):
        if self.fail_noop:
            self.fail_noop -= 1
            raise imaplib.IMAP4.abort(b"connection closed")
        return "OK", [b"ok"]

    def capability(self):
        return "OK", [" ".join(sorted(self.caps)).encode()]

    def list(self, directory='""', pattern="*"):
        lines = [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.store]
        return "OK", lines

    def select(self, mailbox="INBOX", readonly=False):
        if mailbox not in self.store:
            raise imaplib.IMAP4.error(b"Mailbox does not exist")
        self.selected = mailbox
        self.readonly = readonly
        return "OK", [str(len(self.store[mailbox])).encode()]

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            return self._search(args)
        if command == "FETCH":
            return self._fetch(args[0], args[1] if len(args) > 1 else "")
        if command == "STORE":
            return self._store(args)
        if command == "MOVE":
            return self._move(args[0], args[1])
        if command == "COPY":
            return self._copy(args[0], args[1])
        if command == "EXPUNGE":
            return self._expunge(args[0])
        raise imaplib.IMAP4.error(b"unsupported")

    def append(self, mailbox, flags, date_time, message):
        box = self.store.setdefault(mailbox, {})
        uid = (max(box) if box else 0) + 1
        box[uid] = {
            "raw": message if isinstance(message, bytes) else bytes(message),
            "internal": "07-Nov-2026 12:00:00 -0700",
            "flags": {"\\Seen"},
        }
        return "OK", [b"appended"]

    def _search(self, args):
        self.searches.append(args)
        box = self.store[self.selected]
        if args and args[0] == "HEADER":
            needle = args[-1].strip('"')
            hits = [str(uid) for uid, msg in box.items() if needle.encode() in msg["raw"]]
            return "OK", [" ".join(hits).encode()]
        criteria = " ".join(str(part) for part in args)
        import re

        sender = re.search(r'FROM "((?:\\.|[^"])*)"', criteria)
        hits = []
        for uid, msg in sorted(box.items()):
            raw = msg["raw"].decode("utf-8", "replace")
            if sender and sender.group(1).replace('\\"', '"').lower() not in raw.lower():
                continue
            hits.append(str(uid))
        return "OK", [" ".join(hits).encode()]

    def _fetch(self, uid_set, spec):
        uid = int(str(uid_set))
        msg = self.store[self.selected][uid]
        raw = msg["raw"]
        if "BODY.PEEK[]" in spec or spec == "(BODY.PEEK[])":
            meta = f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode()
            return "OK", [(meta, raw), b")"]
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        header_lines = []
        for key in ("Date", "From", "Subject", "To", "Message-ID"):
            if parsed.get(key):
                header_lines.append(f"{key}: {parsed[key]}")
        header = ("\r\n".join(header_lines) + "\r\n").encode()
        body = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else raw.split(b"\n\n", 1)[-1]
        limit = 800
        sized = re.search(r"<0\.(\d+)>", spec)
        if sized:
            limit = int(sized.group(1))
        text = body[:limit]
        internal = msg["internal"]
        meta1 = (
            f'{uid} (UID {uid} INTERNALDATE "{internal}" '
            f"BODY[HEADER.FIELDS (DATE FROM SUBJECT)] {{{len(header)}}}"
        ).encode()
        meta2 = f" BODY[TEXT]<0> {{{len(text)}}}".encode()
        return "OK", [(meta1, header), (meta2, text), b")"]

    def _store(self, args):
        uid = int(args[0])
        action = args[1]
        flag = args[2].strip("()").strip("\\")
        flag = "\\" + flag
        flags = self.store[self.selected][uid].setdefault("flags", set())
        if action.startswith("+"):
            flags.add(flag)
        else:
            flags.discard(flag)
        return "OK", [b"ok"]

    def _move(self, uid, dest):
        dest = dest.strip().strip('"')
        uid = int(uid)
        msg = self.store[self.selected].pop(uid)
        self.store.setdefault(dest, {})[uid] = msg
        return "OK", [b"ok"]

    def _copy(self, uid, dest):
        dest = dest.strip().strip('"')
        uid = int(uid)
        self.store.setdefault(dest, {})[uid] = dict(self.store[self.selected][uid])
        return "OK", [b"ok"]

    def _expunge(self, uid):
        uid = int(uid)
        flags = self.store[self.selected][uid].get("flags", set())
        if "\\Deleted" in flags:
            del self.store[self.selected][uid]
        return "OK", [b"ok"]


class FakeSMTP:
    def __init__(self, sink: list, credential: str) -> None:
        self.sink = sink
        self.credential = credential

    def __call__(self, host, port, timeout=None):
        parent = self

        class _SMTP:
            def ehlo(self):
                return (250, b"ok")

            def starttls(self, context=None):
                return (220, b"ok")

            def login(self, user, presented):
                if presented != parent.credential:
                    raise smtplib.SMTPAuthenticationError(535, b"no")

            def send_message(self, message):
                parent.sink.append(message)

            def quit(self):
                return (221, b"bye")

        return _SMTP()


def raw_message(subject="Hello", sender="Ada <ada@example.com>", body="Plain body", html=None, attachment=None, message_id="<m@example.com>"):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "you@icloud.com"
    msg["Subject"] = subject
    msg["Date"] = "Sat, 07 Nov 2026 10:00:00 -0700"
    msg["Message-ID"] = message_id
    if html and body is None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body or "")
        if html:
            msg.add_alternative(html, subtype="html")
    if attachment:
        name, data, ctype = attachment
        main, sub = ctype.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes()


class FakeCloud:
    """Scripted CalDAV and CardDAV. Transport signature matches DavClient."""

    def __init__(self, user: str, credential: str) -> None:
        self.user = user
        self.credential = credential
        self.calls: list[tuple[str, str, str]] = []
        self.feeds: dict[str, bytes] = {}
        self.evil = False
        self.redirect_once = False
        self.reject_anyof = False
        self._etag = 1
        self.calendars = {
            "work": {"name": "Work Calendar", "components": ["VEVENT"], "items": {}},
            "home": {"name": "Home", "components": ["VEVENT"], "items": {}},
            "personal": {"name": "Personal", "components": ["VTODO"], "items": {}},
        }
        self.book = {"name": "Contacts", "items": {}}

    def __call__(self, method, url, headers, body, timeout):
        text = (body or b"").decode("utf-8", "replace")
        self.calls.append((method, url, text))
        if method == "GET" and url in self.feeds:
            return 200, {"content-type": "text/calendar"}, self.feeds[url]
        if not self._authorized(headers):
            return 401, {}, b"denied"
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if self.evil and parsed.path in ("", "/"):
            return 301, {"location": "https://evil.example/steal"}, b""
        if self.redirect_once and parsed.path in ("", "/") and "p21-" not in host:
            self.redirect_once = False
            target = "https://p21-caldav.icloud.com/" if "contact" not in host else "https://p21-contacts.icloud.com/"
            return 301, {"location": target}, b""
        return self._dispatch(method, host, parsed.path, headers, text)

    def _authorized(self, headers) -> bool:
        header = headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode()
        except Exception:
            return False
        user, _, presented = raw.partition(":")
        return user == self.user and presented == self.credential

    def _next_etag(self) -> str:
        self._etag += 1
        return f'"{self._etag}"'

    def add_event(self, calendar: str, uid: str, summary: str, start: datetime, end: datetime) -> None:
        event = Component("VEVENT")
        event.set("UID", uid)
        event.set("SUMMARY", summary)
        start_token, start_params = write_when(start, False)
        end_token, end_params = write_when(end, False)
        event.set("DTSTART", start_token, start_params)
        event.set("DTEND", end_token, end_params)
        body = serialize(wrap_calendar(event))
        self.calendars[calendar]["items"][uid] = {"etag": self._next_etag(), "body": body}

    def add_todo(self, list_key: str, uid: str, title: str, description: str = "") -> None:
        todo = Component("VTODO")
        todo.set("UID", uid)
        todo.set("SUMMARY", title)
        todo.set("STATUS", "NEEDS-ACTION")
        if description:
            todo.set("DESCRIPTION", description)
        body = serialize(wrap_calendar(todo))
        self.calendars[list_key]["items"][uid] = {"etag": self._next_etag(), "body": body}

    def add_contact(self, uid: str, full_name: str, email: str = "", phone: str = "") -> None:
        card = Card()
        card.set_single("VERSION", "3.0")
        card.set_single("UID", uid)
        card.set_single("FN", full_name)
        card.set_single("N", f"{full_name};;;;")
        if email:
            card.set_many("EMAIL", [(email, {"TYPE": "INTERNET"})])
        if phone:
            card.set_many("TEL", [(phone, {"TYPE": "CELL"})])
        self.book["items"][uid] = {"etag": self._next_etag(), "body": serialize_vcard(card)}

    def _dispatch(self, method, host, path, headers, body):
        card = "contact" in host
        path = path or "/"
        if method == "PROPFIND" and path == "/":
            if card:
                href = "/1842/card-principal/"
            else:
                href = "/1842/principal/"
            return 207, {}, self._principal(href).encode()
        if method == "PROPFIND" and path.rstrip("/").endswith("principal"):
            home = "/1842/card/" if card else "/1842/calendars/"
            prop = "addressbook-home-set" if card else "calendar-home-set"
            ns = "urn:ietf:params:xml:ns:carddav" if card else "urn:ietf:params:xml:ns:caldav"
            return 207, {}, self._home(prop, ns, home).encode()
        if method == "PROPFIND" and path.rstrip("/") in {"/1842/calendars", "/1842/card"}:
            return 207, {}, self._collection(card).encode()
        if method == "REPORT":
            key = path.rstrip("/").rsplit("/", 1)[-1]
            cal = self.calendars.get(key)
            if cal and cal.get("report_fails"):
                return 403, {}, b"report unsupported"
            if self.reject_anyof and "anyof" in body:
                self.reject_anyof = False
                return 400, {}, b"anyof unsupported"
            return 207, {}, self._report(card, path, body).encode()
        if method == "PUT":
            return self._put(card, path, headers, body)
        if method == "DELETE":
            return self._delete(path, headers)
        return 404, {}, b"missing"

    def _principal(self, href: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop>'
            f"<d:current-user-principal><d:href>{href}</d:href></d:current-user-principal>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
        )

    def _home(self, prop: str, ns: str, href: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:multistatus xmlns:d="DAV:" xmlns:x="{ns}"><d:response><d:href>/</d:href>'
            f"<d:propstat><d:prop><x:{prop}><d:href>{href}</d:href></x:{prop}>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
        )

    def _collection(self, card: bool) -> str:
        rows = []
        if card:
            rows.append(self._resource("/1842/card/contacts/", "Contacts", "addressbook", []))
        else:
            for key, cal in self.calendars.items():
                kind = "subscribed" if cal.get("subscribed") else "calendar"
                rows.append(
                    self._resource(
                        f"/1842/calendars/{key}/",
                        cal["name"],
                        kind,
                        cal["components"],
                        cal.get("feed", ""),
                    )
                )
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
            'xmlns:card="urn:ietf:params:xml:ns:carddav">'
            + "".join(rows)
            + "</d:multistatus>"
        )

    def _resource(self, href: str, name: str, kind: str, components: list[str], feed: str = "") -> str:
        comps = "".join(f'<c:comp name="{item}"/>' for item in components)
        if kind == "addressbook":
            tag = "card:addressbook"
        elif kind == "subscribed":
            tag = 'cs:subscribed xmlns:cs="http://calendarserver.org/ns/"'
        else:
            tag = "c:calendar"
        source = ""
        if feed:
            source = (
                '<cs:source xmlns:cs="http://calendarserver.org/ns/">'
                f"<d:href>{_xml(feed)}</d:href></cs:source>"
            )
        return (
            "<d:response>"
            f"<d:href>{href}</d:href><d:propstat><d:prop>"
            f"<d:displayname>{_xml(name)}</d:displayname>"
            f"<d:resourcetype><d:collection/><{tag}/></d:resourcetype>"
            f"<c:supported-calendar-component-set>{comps}</c:supported-calendar-component-set>"
            f"{source}"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    def _report(self, card: bool, path: str, body: str) -> str:
        if card:
            query = self._text_match(body)
            rows = []
            for uid, item in self.book["items"].items():
                if query and query not in item["body"] and query not in uid:
                    continue
                rows.append(self._data_row(f"/1842/card/contacts/{uid}.vcf", item, "address-data", "card"))
        else:
            key = path.rstrip("/").rsplit("/", 1)[-1]
            cal = self.calendars.get(key)
            rows = []
            if cal:
                component = "VTODO" if "VTODO" in body else "VEVENT"
                query = self._text_match(body)
                start, end = self._time_range(body)
                for uid, item in cal["items"].items():
                    if query and query not in item["body"] and query not in uid:
                        continue
                    if component not in item["body"]:
                        continue
                    if start and end and not self._overlaps(item["body"], start, end):
                        continue
                    rows.append(self._data_row(f"/1842/calendars/{key}/{uid}.ics", item, "calendar-data", "c"))
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
            'xmlns:card="urn:ietf:params:xml:ns:carddav">'
            + "".join(rows)
            + "</d:multistatus>"
        )

    def _text_match(self, body: str) -> str:
        marker = "<card:text-match"
        if "text-match" not in body:
            return ""
        start = body.find(">", body.find("text-match"))
        end = body.find("</", start)
        if start < 0 or end < 0:
            return ""
        raw = body[start + 1 : end]
        return raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')

    def _time_range(self, body: str):
        if "time-range" not in body:
            return None, None
        import re

        match = re.search(r'start="(\d{8}T\d{6}Z)" end="(\d{8}T\d{6}Z)"', body)
        if not match:
            return None, None
        fmt = "%Y%m%dT%H%M%SZ"
        start = datetime.strptime(match.group(1), fmt).replace(tzinfo=timezone.utc)
        end = datetime.strptime(match.group(2), fmt).replace(tzinfo=timezone.utc)
        return start, end

    def _overlaps(self, ics: str, start: datetime, end: datetime) -> bool:
        try:
            root = parse_ics(ics)
        except ValueError:
            return False
        for child in root.children:
            if child.name not in {"VEVENT", "VTODO"}:
                continue
            prop = child.first("DTSTART")
            if prop is None:
                continue
            event_start, _all_day = parse_when(prop)
            if not isinstance(event_start, datetime):
                event_start = datetime(event_start.year, event_start.month, event_start.day, tzinfo=timezone.utc)
            end_prop = child.first("DTEND")
            if end_prop is None:
                event_end = event_start + timedelta(hours=1)
            else:
                event_end, _ = parse_when(end_prop)
                if not isinstance(event_end, datetime):
                    event_end = datetime(event_end.year, event_end.month, event_end.day, tzinfo=timezone.utc)
            if event_start < end and event_end > start:
                return True
        return False

    def _data_row(self, href: str, item: dict, data_name: str, prefix: str) -> str:
        return (
            "<d:response>"
            f"<d:href>{href}</d:href><d:propstat><d:prop>"
            f"<d:getetag>{_xml(item['etag'])}</d:getetag>"
            f"<{prefix}:{data_name}>{_xml(item['body'])}</{prefix}:{data_name}>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    def _bucket(self, path: str):
        parts = [part for part in path.split("/") if part]
        if "card" in parts:
            return self.book["items"], "vcf"
        # /1842/calendars/ess/uid.ics
        if len(parts) >= 3 and parts[1] == "calendars":
            cal = self.calendars.get(parts[2])
            if cal:
                return cal["items"], "ics"
        return None, ""

    def _put(self, card, path, headers, body):
        if self.reject_anyof:
            pass
        items, _kind = self._bucket(path)
        if items is None:
            return 404, {}, b"no collection"
        name = path.rstrip("/").rsplit("/", 1)[-1]
        uid = name.rsplit(".", 1)[0]
        if headers.get("If-None-Match") == "*" and uid in items:
            return 412, {}, b"exists"
        if "If-Match" in headers and uid in items and headers["If-Match"] != items[uid]["etag"]:
            return 412, {}, b"stale"
        if "If-Match" in headers and uid not in items:
            return 404, {}, b"missing"
        items[uid] = {"etag": self._next_etag(), "body": body}
        return 201, {}, b""

    def _delete(self, path, headers):
        items, _kind = self._bucket(path)
        if items is None:
            return 404, {}, b"missing"
        name = path.rstrip("/").rsplit("/", 1)[-1]
        uid = name.rsplit(".", 1)[0]
        if uid not in items:
            return 404, {}, b"missing"
        if headers.get("If-Match") and headers["If-Match"] != items[uid]["etag"]:
            return 412, {}, b"stale"
        del items[uid]
        return 204, {}, b""
