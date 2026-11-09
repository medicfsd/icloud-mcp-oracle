"""Small HTML-to-text conversion for mail bodies. Stdlib only."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP = {"script", "style"}
_BREAK = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "li", "table"}
_MAX_HTML = 1_000_000


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        if tag in _BREAK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self.skip:
            self.skip -= 1
        if tag in {"p", "div", "tr", "h1", "h2", "h3", "h4", "table"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    sample = html[:_MAX_HTML]
    parser = _Text()
    try:
        parser.feed(sample)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", sample)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def snippet(text: str, limit: int) -> str:
    cleaned = html_to_text(text) if "<" in text[:400] and ">" in text[:800] else text
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"
