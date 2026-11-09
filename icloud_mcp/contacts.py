"""CardDAV contacts. Search matches name, email, phone, or organization."""

from __future__ import annotations

import uuid

from icloud_mcp.dav import (
    DavClient,
    addressbook_one_field_xml,
    addressbook_query_xml,
    addressbook_uid_xml,
    join_child,
)
from icloud_mcp.errors import ServiceError
from icloud_mcp.ical import utc_stamp
from icloud_mcp.vcard import Card, n_from_full_name, parse_vcard, public_contact, serialize_vcard

MAX_CARD_BYTES = 1_000_000


def _check_uid(uid: str) -> str:
    text = uid.strip()
    if not text or len(text) > 400 or any(char in text for char in "\r\n\x00<>"):
        raise ServiceError("That contact uid is not usable.")
    return text


def _check_name(name: str) -> str:
    text = name.strip()
    if not text or len(text) > 200 or any(char in text for char in "\r\n"):
        raise ServiceError("The contact name is missing, too long, or has a newline.")
    return text


def _optional(value: str | None, limit: int, label: str) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    if any(char in text for char in "\r\n") and label != "note":
        raise ServiceError(f"The contact {label} cannot contain a newline.")
    if len(text) > limit:
        raise ServiceError(f"The contact {label} is too long.")
    return text


def _matches(contact: dict, query: str) -> bool:
    needle = query.lower()
    fields = [str(contact.get("full_name") or ""), str(contact.get("organization") or ""), str(contact.get("note") or "")]
    fields.extend(str(item) for item in contact.get("emails") or [])
    fields.extend(str(item) for item in contact.get("phones") or [])
    return any(needle in field.lower() for field in fields)


class ContactsService:
    def __init__(self, dav: DavClient) -> None:
        self.dav = dav
        self.secrets = dav.secrets

    def _books(self) -> list[dict]:
        books = self.dav.list_addressbooks()
        if not books:
            raise ServiceError("No iCloud address book was found.")
        return books

    def _report_query(self, book: dict, query: str) -> list[dict]:
        try:
            return self.dav.report(book["href"], addressbook_query_xml(query), "address-data")
        except ServiceError as exc:
            if "HTTP 400" not in str(exc) and "HTTP 415" not in str(exc):
                raise
        found: list[dict] = []
        seen: set[str] = set()
        for prop in ("FN", "EMAIL", "TEL", "ORG"):
            for item in self.dav.report(book["href"], addressbook_one_field_xml(prop, query), "address-data"):
                if item["href"] not in seen:
                    seen.add(item["href"])
                    found.append(item)
        return found

    def search(self, query: str, limit: int = 25) -> dict:
        text = query.strip()
        if len(text) < 2 or len(text) > 200:
            raise ServiceError("The contact search needs between 2 and 200 characters.")
        limit = max(1, min(int(limit), 50))
        rows = []
        seen: set[str] = set()
        for book in self._books():
            for item in self._report_query(book, text):
                try:
                    card = parse_vcard(item["data"])
                except ValueError:
                    continue
                contact = public_contact(card)
                if not contact["uid"] or contact["uid"] in seen or not _matches(contact, text):
                    continue
                seen.add(str(contact["uid"]))
                contact.pop("note", None)
                rows.append(contact)
        return {"query": text, "truncated": len(rows) > limit, "contacts": rows[:limit]}

    def _find(self, uid: str):
        uid = _check_uid(uid)
        for book in self._books():
            items = self.dav.report(book["href"], addressbook_uid_xml(uid), "address-data")
            for item in items:
                try:
                    card = parse_vcard(item["data"])
                except ValueError:
                    continue
                if card.text("UID") == uid:
                    return book, item, card
        raise ServiceError(f"No contact with uid {uid!r}.")

    def get(self, uid: str) -> dict:
        _book, _item, card = self._find(uid)
        return public_contact(card)

    def _put_card(self, href: str, card: Card, *, etag: str | None, create: bool) -> None:
        payload = serialize_vcard(card).encode("utf-8")
        if len(payload) > MAX_CARD_BYTES:
            raise ServiceError(
                "That contact is too large to write safely, often because of a photo. Nothing was changed."
            )
        self.dav.put(href, payload, "text/vcard; charset=utf-8", etag=etag, create=create)

    def create(
        self,
        full_name: str,
        email: str | None = None,
        phone: str | None = None,
        organization: str | None = None,
        note: str | None = None,
    ) -> dict:
        book = self._books()[0]
        name = _check_name(full_name)
        email = _optional(email, 320, "email")
        phone = _optional(phone, 40, "phone")
        organization = _optional(organization, 200, "organization")
        note = _optional(note, 2000, "note")
        uid = str(uuid.uuid4())
        card = Card()
        card.set_single("VERSION", "3.0")
        card.set_single("UID", uid)
        card.set_single("FN", name)
        card.set_single("N", n_from_full_name(name))
        card.set_single("REV", utc_stamp())
        if email:
            card.set_many("EMAIL", [(email, {"TYPE": "INTERNET"})])
        if phone:
            card.set_many("TEL", [(phone, {"TYPE": "CELL"})])
        if organization:
            card.set_single("ORG", organization)
        if note:
            card.set_single("NOTE", note)
        self._put_card(join_child(book["href"], uid + ".vcf"), card, etag=None, create=True)
        created = public_contact(card)
        created["created"] = True
        return created

    def update(
        self,
        uid: str,
        full_name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        organization: str | None = None,
        note: str | None = None,
    ) -> dict:
        if all(value is None for value in (full_name, email, phone, organization, note)):
            raise ServiceError("Pass at least one field to change.")
        _book, item, card = self._find(uid)
        if full_name is not None:
            name = _check_name(full_name)
            card.set_single("FN", name)
            card.set_single("N", n_from_full_name(name))
        if email is not None:
            email = _optional(email, 320, "email") or ""
            card.set_many("EMAIL", [(email, {"TYPE": "INTERNET"})] if email else [])
        if phone is not None:
            phone = _optional(phone, 40, "phone") or ""
            card.set_many("TEL", [(phone, {"TYPE": "CELL"})] if phone else [])
        if organization is not None:
            card.set_single("ORG", _optional(organization, 200, "organization") or "")
        if note is not None:
            card.set_single("NOTE", _optional(note, 2000, "note") or "")
        card.set_single("REV", utc_stamp())
        self._put_card(item["href"], card, etag=item["etag"], create=False)
        updated = public_contact(card)
        updated["updated"] = True
        return updated
