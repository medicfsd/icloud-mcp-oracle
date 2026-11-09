"""CalDAV and CardDAV client for iCloud.

Discovery follows current-user-principal, then calendar-home-set or
addressbook-home-set. The same app-specific password as mail is sent as
HTTP Basic, and only to hosts under icloud.com.
"""

from __future__ import annotations

import base64
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable

from icloud_mcp.config import CALDAV_URL, CARDDAV_URL
from icloud_mcp.errors import ServiceError, redact
from icloud_mcp.http import urllib_transport

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]

DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
CARD = "urn:ietf:params:xml:ns:carddav"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _parse_xml(payload: bytes) -> ET.Element:
    head = payload[:500].lstrip().lower()
    if head.startswith(b"<!doctype") or b"<!entity" in payload[:2000].lower():
        raise ServiceError("iCloud returned a document the connector will not parse.")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ServiceError("iCloud returned a response that was not valid XML.") from exc


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) == name]


def _href_text(element: ET.Element) -> str:
    for node in element.iter():
        if _local(node.tag) == "href" and node.text:
            return node.text.strip()
    return ""


def _prop_href(response: ET.Element, prop_name: str) -> str:
    for node in response.iter():
        if _local(node.tag) == prop_name:
            href = _href_text(node)
            if href:
                return href
    return ""


def _text(response: ET.Element, name: str) -> str:
    for node in response.iter():
        if _local(node.tag) == name and node.text:
            return node.text.strip()
    return ""


def _resourcetype_has(response: ET.Element, name: str) -> bool:
    for node in response.iter():
        if _local(node.tag) != "resourcetype":
            continue
        for child in node.iter():
            if child is not node and _local(child.tag) == name:
                return True
    return False


def _components(response: ET.Element) -> list[str]:
    names: list[str] = []
    for node in response.iter():
        if _local(node.tag) == "comp" and node.attrib.get("name"):
            names.append(node.attrib["name"].upper())
    return names


def apple_host(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and (host == "icloud.com" or host.endswith(".icloud.com"))


def join_child(base: str, name: str) -> str:
    if not base.endswith("/"):
        base += "/"
    return urllib.parse.urljoin(base, urllib.parse.quote(name, safe=""))


def resolve_by_name(items: list[dict], name: str, kind: str) -> dict:
    wanted = name.strip()
    if not wanted:
        raise ServiceError(f"Name the {kind}.")
    exact = [item for item in items if item["name"].lower() == wanted.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ServiceError(f"More than one {kind} is named {wanted!r}.")
    partial = [item for item in items if wanted.lower() in item["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    available = ", ".join(item["name"] for item in items) or "(none)"
    if not partial:
        raise ServiceError(f"No {kind} named {wanted!r}. Available: {available}")
    listed = ", ".join(item["name"] for item in partial)
    raise ServiceError(f"{wanted!r} matches more than one {kind}: {listed}")


def _propfind(props: str) -> bytes:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
        'xmlns:card="urn:ietf:params:xml:ns:carddav" '
        'xmlns:cs="http://calendarserver.org/ns/">'
        f"<d:prop>{props}</d:prop></d:propfind>"
    )
    return body.encode("utf-8")


def https_feed_url(url: str) -> str:
    """webcal and http feeds are read as https. The address is not logged."""
    parsed = urllib.parse.urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme in {"webcal", "webcals", "http"}:
        parsed = parsed._replace(scheme="https")
    elif scheme != "https" or not parsed.hostname:
        raise ServiceError("Could not read the subscribed calendar.")
    if not parsed.hostname:
        raise ServiceError("Could not read the subscribed calendar.")
    return urllib.parse.urlunparse(parsed)


def calendar_query_xml(*, component: str, start: str | None, end: str | None, uid: str | None) -> bytes:
    if component not in {"VEVENT", "VTODO"}:
        raise ServiceError("Unsupported calendar component.")
    inner = ""
    if uid is not None:
        inner = (
            "<c:prop-filter name=\"UID\">"
            f'<c:text-match collation="i;octet">{xml_escape(uid)}</c:text-match>'
            "</c:prop-filter>"
        )
    elif start and end:
        inner = f'<c:time-range start="{start}" end="{end}"/>'
    expand = ""
    if start and end and uid is None:
        expand = f'<c:expand start="{start}" end="{end}"/>'
    data = f"<c:calendar-data>{expand}</c:calendar-data>" if expand else "<c:calendar-data/>"
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f"<d:prop><d:getetag/>{data}</d:prop>"
        "<c:filter>"
        '<c:comp-filter name="VCALENDAR">'
        f'<c:comp-filter name="{component}">{inner}</c:comp-filter>'
        "</c:comp-filter></c:filter></c:calendar-query>"
    )
    return body.encode("utf-8")


def addressbook_query_xml(query: str) -> bytes:
    fields = ("FN", "EMAIL", "TEL", "N", "ORG", "NICKNAME")
    filters = "".join(
        f'<card:prop-filter name="{name}">'
        '<card:text-match collation="i;unicode-casemap" match-type="contains">'
        f"{xml_escape(query)}</card:text-match></card:prop-filter>"
        for name in fields
    )
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:getetag/><card:address-data/></d:prop>"
        f'<card:filter test="anyof">{filters}</card:filter>'
        "</card:addressbook-query>"
    )
    return body.encode("utf-8")


def addressbook_one_field_xml(prop: str, query: str) -> bytes:
    if prop not in {"FN", "EMAIL", "TEL", "N", "ORG", "NICKNAME"}:
        raise ServiceError("Unsupported contact field.")
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:getetag/><card:address-data/></d:prop>"
        f'<card:filter><card:prop-filter name="{prop}">'
        '<card:text-match collation="i;unicode-casemap" match-type="contains">'
        f"{xml_escape(query)}</card:text-match></card:prop-filter></card:filter>"
        "</card:addressbook-query>"
    )
    return body.encode("utf-8")


def addressbook_uid_xml(uid: str) -> bytes:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:getetag/><card:address-data/></d:prop>"
        "<card:filter><card:prop-filter name=\"UID\">"
        f'<card:text-match collation="i;octet">{xml_escape(uid)}</card:text-match>'
        "</card:prop-filter></card:filter></card:addressbook-query>"
    )
    return body.encode("utf-8")


class DavClient:
    def __init__(
        self,
        user: str,
        credential: str,
        timeout: float = 30,
        transport: Transport | None = None,
        caldav_url: str = CALDAV_URL,
        carddav_url: str = CARDDAV_URL,
    ) -> None:
        self.user = user
        self.credential = credential
        self.timeout = timeout
        self.transport = transport or urllib_transport
        self.caldav_url = caldav_url
        self.carddav_url = carddav_url
        self.secrets = [credential]
        self._lock = threading.Lock()
        self._calendar_home: str | None = None
        self._card_home: str | None = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        token = base64.b64encode(f"{self.user}:{self.credential}".encode("utf-8")).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "User-Agent": "icloud-mcp",
            "Content-Type": "application/xml; charset=utf-8",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200, 201, 204, 207),
    ) -> tuple[str, dict[str, str], bytes]:
        current = url
        for _ in range(5):
            if not apple_host(current):
                raise ServiceError("Refusing to send the app-specific password to a non-iCloud host.")
            try:
                status, headers, payload = self.transport(
                    method, current, self._headers(extra_headers), body, self.timeout
                )
            except ServiceError:
                raise
            except Exception as exc:
                raise ServiceError("Could not reach iCloud: " + redact(str(exc), self.secrets)) from None
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location", "")
                if not location:
                    raise ServiceError("iCloud redirected without a destination.")
                current = urllib.parse.urljoin(current, location)
                continue
            if status == 401:
                raise ServiceError(
                    "iCloud rejected the app-specific password for calendar or contacts. "
                    "Check ICLOUD_USER and ICLOUD_APP_PASSWORD."
                )
            if status not in ok:
                detail = payload[:180].decode("utf-8", "replace").replace("\n", " ")
                detail = redact(detail, self.secrets)
                raise ServiceError(f"iCloud answered HTTP {status} for {method}. {detail}".strip())
            return current, headers, payload
        raise ServiceError("iCloud redirected too many times.")

    def _home(self, root_url: str, home_prop: str, cache_name: str) -> str:
        with self._lock:
            cached = getattr(self, cache_name)
        if cached:
            return cached
        final, _, payload = self._request(
            "PROPFIND",
            root_url,
            _propfind("<d:current-user-principal/>"),
            {"Depth": "0"},
        )
        root = _parse_xml(payload)
        principal = ""
        for response in root.iter():
            if _local(response.tag) == "response":
                principal = _prop_href(response, "current-user-principal")
                if principal:
                    break
        if not principal:
            raise ServiceError("iCloud did not return a user principal. The app-specific password may lack this service.")
        principal_url = urllib.parse.urljoin(final, principal)
        final, _, payload = self._request(
            "PROPFIND",
            principal_url,
            _propfind(f"<{home_prop}/>"),
            {"Depth": "0"},
        )
        root = _parse_xml(payload)
        home = ""
        for response in root.iter():
            if _local(response.tag) == "response":
                home = _prop_href(response, _prop_local(home_prop))
                if home:
                    break
        if not home:
            raise ServiceError("iCloud did not return a calendar or address-book home.")
        resolved = urllib.parse.urljoin(final, home)
        if not resolved.endswith("/"):
            resolved += "/"
        with self._lock:
            setattr(self, cache_name, resolved)
        return resolved

    def calendar_home(self) -> str:
        return self._home(self.caldav_url, "c:calendar-home-set", "_calendar_home")

    def card_home(self) -> str:
        return self._home(self.carddav_url, "card:addressbook-home-set", "_card_home")

    def list_collections(self, home: str, *kinds: str) -> list[dict]:
        _, _, payload = self._request(
            "PROPFIND",
            home,
            _propfind(
                "<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/><cs:source/>"
            ),
            {"Depth": "1"},
        )
        root = _parse_xml(payload)
        found: list[dict] = []
        home_path = urllib.parse.urlparse(home).path.rstrip("/")
        for response in list(root):
            if _local(response.tag) != "response":
                continue
            if not any(_resourcetype_has(response, kind) for kind in kinds):
                continue
            href = _href_text(response)
            if not href:
                continue
            absolute = urllib.parse.urljoin(home, href)
            if urllib.parse.urlparse(absolute).path.rstrip("/") == home_path:
                continue
            if not absolute.endswith("/"):
                absolute += "/"
            name = _text(response, "displayname") or urllib.parse.unquote(absolute.rstrip("/").rsplit("/", 1)[-1])
            subscribed = _resourcetype_has(response, "subscribed")
            row = {
                "name": name,
                "href": absolute,
                "components": _components(response),
                "subscribed": subscribed,
                "read_only": subscribed,
            }
            if subscribed:
                feed = _prop_href(response, "source")
                if feed:
                    row["feed_url"] = urllib.parse.urljoin(absolute, feed)
            found.append(row)
        found.sort(key=lambda item: item["name"].lower())
        return found

    def list_calendars(self) -> list[dict]:
        return self.list_collections(self.calendar_home(), "calendar", "subscribed")

    def list_addressbooks(self) -> list[dict]:
        return self.list_collections(self.card_home(), "addressbook")

    def fetch_feed(self, url: str) -> bytes:
        """GET a subscribed calendar's ICS feed. No Apple credential is sent, and the address is not logged."""
        try:
            current = https_feed_url(url)
        except ServiceError:
            raise ServiceError("Could not read the subscribed calendar.") from None
        for _ in range(5):
            if urllib.parse.urlparse(current).scheme != "https":
                raise ServiceError("Could not read the subscribed calendar.")
            try:
                status, headers, payload = self.transport(
                    "GET",
                    current,
                    {"User-Agent": "icloud-mcp"},
                    None,
                    self.timeout,
                )
            except Exception:
                raise ServiceError("Could not read the subscribed calendar.") from None
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location", "")
                if not location:
                    raise ServiceError("Could not read the subscribed calendar.")
                current = urllib.parse.urljoin(current, location)
                continue
            if status != 200 or not payload:
                raise ServiceError("Could not read the subscribed calendar.")
            return payload
        raise ServiceError("Could not read the subscribed calendar.")

    def report(self, href: str, body: bytes, data_name: str) -> list[dict]:
        final, _, payload = self._request("REPORT", href, body, {"Depth": "1"})
        root = _parse_xml(payload)
        items: list[dict] = []
        for response in list(root):
            if _local(response.tag) != "response":
                continue
            data = _nested_text(response, data_name)
            if not data:
                continue
            item_href = urllib.parse.urljoin(final, _href_text(response))
            items.append({"href": item_href, "etag": _text(response, "getetag"), "data": data})
        return items

    def put(
        self,
        href: str,
        body: bytes,
        content_type: str,
        *,
        etag: str | None = None,
        create: bool = False,
    ) -> None:
        headers = {"Content-Type": content_type}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        try:
            self._request("PUT", href, body, headers)
        except ServiceError as exc:
            if "HTTP 412" in str(exc):
                raise ServiceError("That item changed in iCloud. Read it again, then retry. Nothing else was written.") from None
            raise

    def delete(self, href: str, etag: str) -> None:
        headers = {"If-Match": etag} if etag else {}
        try:
            self._request("DELETE", href, None, headers)
        except ServiceError as exc:
            text = str(exc)
            if "HTTP 404" in text:
                raise ServiceError("That item is no longer in iCloud.") from None
            if "HTTP 412" in text:
                raise ServiceError("That item changed in iCloud. Read it again before deleting. Nothing was deleted.") from None
            raise


def _prop_local(tag: str) -> str:
    return tag.split(":", 1)[-1]


def _nested_text(response: ET.Element, name: str) -> str:
    for node in response.iter():
        if _local(node.tag) == name:
            if node.text and node.text.strip():
                return node.text.strip()
            # Some servers put the payload in the tail of child nodes only.
            chunks = [node.text or ""]
            for child in list(node):
                chunks.append(ET.tostring(child, encoding="unicode"))
                chunks.append(child.tail or "")
            combined = "".join(chunks).strip()
            if combined:
                return combined
    return ""
