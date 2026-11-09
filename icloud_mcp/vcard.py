"""vCard 3.0 read and write for the contact fields this connector edits."""

from __future__ import annotations

from icloud_mcp.ical import Prop, escape_text, fold, parse_content_line, unfold


class Card:
    def __init__(self, props: list[Prop] | None = None) -> None:
        self.props = props or []

    def values(self, name: str) -> list[str]:
        return [prop.value for prop in self.props if prop.name == name]

    def text(self, name: str, default: str = "") -> str:
        found = self.values(name)
        return found[0] if found else default

    def set_single(self, name: str, value: str, params: dict[str, str] | None = None) -> None:
        params = dict(params or {})
        kept = [prop for prop in self.props if prop.name != name]
        kept.append(Prop(name, params, value))
        self.props = kept

    def set_many(self, name: str, values: list[tuple[str, dict[str, str]]]) -> None:
        kept = [prop for prop in self.props if prop.name != name]
        for value, params in values:
            kept.append(Prop(name, params, value))
        self.props = kept


def parse_vcard(text: str) -> Card:
    props: list[Prop] = []
    started = False
    for line in unfold(text):
        if line.upper().startswith("BEGIN:VCARD"):
            started = True
            continue
        if line.upper().startswith("END:VCARD"):
            break
        if not started:
            continue
        name, params, value = parse_content_line(line)
        props.append(Prop(name, params, value))
    if not started:
        raise ValueError("not a vcard")
    return Card(props)


def serialize_vcard(card: Card) -> str:
    lines = ["BEGIN:VCARD"]
    for prop in card.props:
        pieces = [prop.name]
        for key, val in prop.params.items():
            pieces.append(f"{key}={val}")
        lines.append(fold(";".join(pieces) + ":" + escape_text(prop.value)))
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def n_from_full_name(full_name: str) -> str:
    parts = full_name.split()
    if not parts:
        return ";;;;"
    if len(parts) == 1:
        return f";{parts[0]};;;"
    return f"{parts[-1]};{' '.join(parts[:-1])};;;"


def public_contact(card: Card) -> dict[str, object]:
    return {
        "uid": card.text("UID"),
        "full_name": card.text("FN"),
        "emails": card.values("EMAIL"),
        "phones": card.values("TEL"),
        "organization": card.text("ORG"),
        "note": card.text("NOTE"),
    }
