"""IMAP and SMTP for iCloud Mail.

One IMAP connection is reused and dropped if the server closes it. SMTP is a
new connection per send. Message ids are IMAP UIDs, so they stay valid after
other messages are deleted.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import re
import smtplib
import ssl
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from email.message import EmailMessage

from icloud_mcp.config import IMAP_HOST, IMAP_PORT, SMTP_HOST, SMTP_PORT, Config
from icloud_mcp.dates import AZ, imap_day, parse_mail_day, show_az
from icloud_mcp.errors import ServiceError, redact
from icloud_mcp.htmltext import html_to_text, snippet

BODY_MAX = 50_000
SNIPPET_MAX = 240
PREVIEW_BYTES = 12000
SCAN_MAX = 200
SEARCH_MAX = 50


def quote_imap(value: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise ServiceError("Search text cannot contain newlines.")
    if len(value) > 200:
        raise ServiceError("Search text is too long.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def check_uid(uid: str) -> str:
    text = str(uid).strip()
    if not text.isdigit() or len(text) > 20:
        raise ServiceError("Message id must be the numeric id returned by mail_search_messages.")
    return text


def check_mailbox(name: str) -> str:
    text = name.strip()
    if not text or len(text) > 200 or any(char in text for char in '\r\n\x00"'):
        raise ServiceError("Mailbox name is missing, too long, or contains quotes or newlines.")
    return text


def quote_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\") + '"'


def decode_mutf7(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            out.append(value[index])
            index += 1
            continue
        end = value.find("-", index)
        if end < 0:
            out.append(value[index:])
            break
        chunk = value[index + 1 : end]
        if chunk == "":
            out.append("&")
        else:
            token = "+" + chunk.replace(",", "/") + "-"
            try:
                out.append(token.encode("ascii").decode("utf-7"))
            except (UnicodeDecodeError, UnicodeError):
                out.append(value[index : end + 1])
        index = end + 1
    return "".join(out)


def parse_list_name(line: bytes | str) -> str | None:
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    text = text.strip()
    if text.endswith('"'):
        start = text.rfind('"', 0, len(text) - 1)
        if start >= 0:
            name = text[start + 1 : -1].replace('\\"', '"').replace("\\\\", "\\")
            return decode_mutf7(name)
    parts = text.split(" ")
    return decode_mutf7(parts[-1]) if parts and parts[-1] else None


def build_search_criteria(
    *,
    sender: str | None,
    to: str | None,
    subject: str | None,
    text: str | None,
    since: str | None,
    before: str | None,
    unread: bool,
) -> str:
    parts: list[str] = []
    if unread:
        parts.append("UNSEEN")
    if sender:
        parts.append("FROM " + quote_imap(sender))
    if to:
        parts.append("TO " + quote_imap(to))
    if subject:
        parts.append("SUBJECT " + quote_imap(subject))
    if text:
        parts.append("TEXT " + quote_imap(text))
    if since:
        parts.append("SINCE " + imap_day(parse_mail_day(since)))
    if before:
        parts.append("BEFORE " + imap_day(parse_mail_day(before)))
    return " ".join(parts) if parts else "ALL"


def parse_addresses(value: str | None, field: str) -> list[str]:
    if value is None or not str(value).strip():
        return []
    raw = str(value)
    if any(char in raw for char in "\r\n\x00"):
        raise ServiceError(f"{field} cannot contain newlines.")
    pairs = email.utils.getaddresses([raw])
    found: list[str] = []
    for name, addr in pairs:
        if not addr or addr.count("@") != 1 or any(char.isspace() for char in addr) or len(addr) > 320:
            raise ServiceError(f"{field} has an address that is not usable: {addr or name!r}")
        found.append(email.utils.formataddr((name, addr)) if name else addr)
    if len(found) > 20:
        raise ServiceError(f"{field} has too many addresses.")
    return found


def check_subject(subject: str) -> str:
    text = subject.strip()
    if not text or any(char in text for char in "\r\n\x00") or len(text) > 300:
        raise ServiceError("Subject is missing, too long, or contains a newline.")
    return text


def check_body(body: str) -> str:
    if body is None or not str(body).strip():
        raise ServiceError("The message body is empty.")
    text = str(body).replace("\x00", "")
    if len(text) > 100_000:
        raise ServiceError("The message body is too long.")
    return text


def reply_subject(original: str) -> str:
    text = (original or "").strip()
    if text.lower().startswith("re:"):
        return check_subject(text)
    return check_subject("Re: " + text if text else "Re:")


def _literals(data) -> tuple[bytes, list[bytes]]:
    meta = b""
    chunks: list[bytes] = []
    for part in data or []:
        if part is None:
            continue
        if isinstance(part, tuple):
            meta += b" " + part[0]
            chunks.append(part[1])
        elif isinstance(part, (bytes, bytearray)):
            meta += b" " + bytes(part)
    return meta, chunks


def _header_map(raw_header: bytes) -> email.message.Message:
    return email.message_from_bytes(raw_header + b"\r\n", policy=email.policy.default)


def _estimate_size(part) -> int:
    payload = part.get_payload(decode=False)
    if not isinstance(payload, str):
        return 0
    if part.get("Content-Transfer-Encoding", "").lower() == "base64":
        compact = re.sub(r"\s+", "", payload)
        return (len(compact) * 3) // 4
    return len(payload.encode("utf-8", "replace"))


def _walk_parts(part, prefix: str, found: list[tuple[str, object]]) -> None:
    if part.is_multipart():
        for index, child in enumerate(part.get_payload(), 1):
            child_id = f"{prefix}.{index}" if prefix else str(index)
            _walk_parts(child, child_id, found)
        return
    found.append((prefix or "1", part))


def _attachment_rows(message) -> list[dict]:
    parts: list[tuple[str, object]] = []
    _walk_parts(message, "", parts)
    rows = []
    for part_id, part in parts:
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        content_type = part.get_content_type()
        attached = disposition == "attachment" or (filename and content_type not in {"text/plain", "text/html"})
        if not attached:
            continue
        rows.append(
            {
                "part": part_id,
                "filename": filename or "attachment",
                "content_type": content_type,
                "size": _estimate_size(part),
            }
        )
    return rows


def _body_text(message) -> str:
    plain: list[str] = []
    html_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == "attachment" or part.is_multipart():
                continue
            try:
                content = part.get_content()
            except Exception:
                continue
            if not isinstance(content, str):
                continue
            if part.get_content_type() == "text/html":
                html_parts.append(content)
            elif part.get_content_type() == "text/plain":
                plain.append(content)
    else:
        try:
            content = message.get_content()
        except Exception:
            content = ""
        if message.get_content_type() == "text/html":
            html_parts.append(content if isinstance(content, str) else "")
        elif isinstance(content, str):
            plain.append(content)
    text = "\n".join(piece.strip() for piece in plain if piece.strip())
    if text:
        return text
    if html_parts:
        return html_to_text("\n".join(html_parts))
    return ""


def _boundary_token(text: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if len(stripped) < 3 or not stripped.startswith("--"):
            continue
        token = stripped[2:]
        if token.endswith("--"):
            token = token[:-2]
        token = token.strip().strip('"')
        if not token or any(char.isspace() for char in token) or len(token) > 70:
            continue
        window = "\n".join(lines[index : index + 8]).lower()
        if "content-type:" in window or "content-transfer-encoding:" in window:
            return token
    return None


def _preview_text(raw: bytes) -> str:
    """Turn a BODY[TEXT] fragment into plain text.

    iCloud search fetches the raw MIME body, which starts with boundary lines
    and part headers. Prefer the decoded text/plain part, then HTML as text.
    """
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    boundary = _boundary_token(text)
    if boundary:
        wrapped = (
            "MIME-Version: 1.0\r\n"
            f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n'
        ).encode("utf-8") + raw
        try:
            extracted = _body_text(email.message_from_bytes(wrapped, policy=email.policy.default))
        except Exception:
            extracted = ""
        if extracted.strip():
            return extracted
    lowered = raw[:400].lower()
    if b"content-type:" in lowered or b"content-transfer-encoding:" in lowered:
        start = 0
        match = re.search(br"(?im)^(content-(?:type|transfer-encoding):)", raw)
        if match:
            start = match.start()
        wrapped = b"MIME-Version: 1.0\r\n" + raw[start:]
        try:
            extracted = _body_text(email.message_from_bytes(wrapped, policy=email.policy.default))
        except Exception:
            extracted = ""
        if extracted.strip():
            return extracted
    return text


def _show_header_date(value: str, fallback: datetime | None) -> str:
    if value:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed is not None:
                return show_az(parsed)
        except (TypeError, ValueError, IndexError):
            pass
    if fallback is not None:
        return show_az(fallback)
    return ""


def _parse_internal(meta: bytes) -> datetime | None:
    match = re.search(br'INTERNALDATE "([^"]+)"', meta)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


class MailClient:
    def __init__(self, config: Config, connector: Callable | None = None, smtp_factory: Callable | None = None) -> None:
        self.config = config
        self.secrets = [config.credential]
        self._connector = connector or self._connect
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._imap = None
        self._lock = threading.RLock()

    def _connect(self):
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=self.config.timeout)
        except (OSError, TimeoutError) as exc:
            raise ServiceError("Could not reach imap.mail.me.com. " + redact(str(exc), self.secrets)) from None
        try:
            typ, _ = imap.login(self.config.user, self.config.credential)
        except imaplib.IMAP4.error:
            raise ServiceError(
                "iCloud rejected the mail login. Check ICLOUD_USER and the app-specific password."
            ) from None
        if typ != "OK":
            raise ServiceError("iCloud rejected the mail login. Check ICLOUD_USER and the app-specific password.")
        return imap

    def _drop(self) -> None:
        imap = self._imap
        self._imap = None
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                try:
                    imap.shutdown()
                except Exception:
                    pass

    def _ensure(self):
        if self._imap is not None:
            try:
                self._imap.noop()
                return self._imap
            except (imaplib.IMAP4.abort, OSError, TimeoutError):
                self._drop()
        self._imap = self._connector()
        return self._imap

    def _call(self, fn):
        with self._lock:
            last: Exception | None = None
            for attempt in (1, 2):
                try:
                    return fn(self._ensure())
                except ServiceError:
                    raise
                except (imaplib.IMAP4.abort, OSError, TimeoutError) as exc:
                    last = exc
                    self._drop()
                    if attempt == 2:
                        raise ServiceError("The mail connection dropped. " + redact(str(last), self.secrets)) from None
                except imaplib.IMAP4.error as exc:
                    raise ServiceError(redact(str(exc), self.secrets) or "iCloud mail refused that request.") from None

    def _select(self, imap, mailbox: str, readonly: bool) -> None:
        typ, _ = imap.select(check_mailbox(mailbox), readonly=readonly)
        if typ != "OK":
            raise ServiceError(f"Could not open mailbox {mailbox!r}.")

    def _fetch(self, imap, uid: str, what: str) -> tuple[bytes, list[bytes]]:
        typ, data = imap.uid("FETCH", uid, what)
        if typ != "OK" or not data or data == [None]:
            raise ServiceError(f"Message {uid} is not in that mailbox.")
        return _literals(data)

    def list_mailboxes(self) -> dict:
        def op(imap):
            typ, data = imap.list()
            if typ != "OK":
                raise ServiceError("Could not list mailboxes.")
            names = []
            for line in data or []:
                if not isinstance(line, (bytes, str)):
                    continue
                name = parse_list_name(line)
                if name:
                    names.append(name)
            names = sorted(set(names), key=str.lower)
            return {"mailboxes": [{"name": name} for name in names[:200]]}

        return self._call(op)

    def search(
        self,
        mailbox: str = "INBOX",
        sender: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        text: str | None = None,
        since: str | None = None,
        before: str | None = None,
        unread: bool = False,
        limit: int = 20,
    ) -> dict:
        mailbox = check_mailbox(mailbox)
        limit = max(1, min(int(limit), SEARCH_MAX))
        criteria = build_search_criteria(
            sender=sender, to=to, subject=subject, text=text, since=since, before=before, unread=unread
        )

        def op(imap):
            self._select(imap, mailbox, readonly=True)
            try:
                typ, data = imap.uid("SEARCH", "CHARSET", "UTF-8", criteria)
            except imaplib.IMAP4.error:
                typ, data = imap.uid("SEARCH", criteria)
            if typ != "OK":
                raise ServiceError("The mailbox search failed.")
            raw = data[0] if data else b""
            if raw is None:
                raw = b""
            if isinstance(raw, str):
                raw = raw.encode()
            uids = [part for part in raw.split() if part.isdigit()]
            truncated = len(uids) > SCAN_MAX
            chosen = uids[-SCAN_MAX:]
            rows = []
            for uid in chosen:
                meta, chunks = self._fetch(
                    imap,
                    uid.decode() if isinstance(uid, bytes) else str(uid),
                    "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)] "
                    f"BODY.PEEK[TEXT]<0.{PREVIEW_BYTES}>)",
                )
                header = _header_map(chunks[0] if chunks else b"")
                preview = _preview_text(chunks[1] if len(chunks) > 1 else b"")
                when = _parse_internal(meta)
                rows.append(
                    {
                        "id": (uid.decode() if isinstance(uid, bytes) else str(uid)),
                        "date": _show_header_date(header.get("Date", ""), when),
                        "from": header.get("From", ""),
                        "subject": header.get("Subject", ""),
                        "snippet": snippet(preview, SNIPPET_MAX),
                        "_sort": when or datetime.min.replace(tzinfo=timezone.utc),
                    }
                )
            rows.sort(key=lambda row: row["_sort"], reverse=True)
            limited = rows[:limit]
            for row in limited:
                row.pop("_sort", None)
            return {
                "mailbox": mailbox,
                "count": len(limited),
                "truncated": truncated or len(rows) > limit,
                "messages": limited,
            }

        return self._call(op)

    def get_message(self, mailbox: str, uid: str) -> dict:
        mailbox = check_mailbox(mailbox)
        uid = check_uid(uid)

        def op(imap):
            self._select(imap, mailbox, readonly=True)
            _meta, chunks = self._fetch(imap, uid, "(BODY.PEEK[])")
            if not chunks:
                raise ServiceError(f"Message {uid} had no body.")
            message = email.message_from_bytes(chunks[0], policy=email.policy.default)
            body = _body_text(message)
            truncated = len(body) > BODY_MAX
            return {
                "id": uid,
                "mailbox": mailbox,
                "date": _show_header_date(message.get("Date", ""), None),
                "from": message.get("From", ""),
                "to": message.get("To", ""),
                "cc": message.get("Cc", ""),
                "subject": message.get("Subject", ""),
                "message_id": message.get("Message-ID", ""),
                "in_reply_to": message.get("In-Reply-To", ""),
                "references": message.get("References", ""),
                "reply_to": message.get("Reply-To", ""),
                "body": body[:BODY_MAX],
                "body_truncated": truncated,
                "attachments": _attachment_rows(message),
            }

        return self._call(op)

    def get_attachment(self, mailbox: str, uid: str, part: str) -> dict:
        import base64

        mailbox = check_mailbox(mailbox)
        uid = check_uid(uid)
        part_id = part.strip()
        if not part_id or len(part_id) > 40 or any(char not in "0123456789." for char in part_id):
            raise ServiceError("Attachment part must be the id from mail_get_message, such as 2 or 1.2.")

        def op(imap):
            self._select(imap, mailbox, readonly=True)
            _meta, chunks = self._fetch(imap, uid, "(BODY.PEEK[])")
            if not chunks:
                raise ServiceError(f"Message {uid} had no body.")
            message = email.message_from_bytes(chunks[0], policy=email.policy.default)
            parts: list[tuple[str, object]] = []
            _walk_parts(message, "", parts)
            match = next((item for item in parts if item[0] == part_id), None)
            if match is None:
                raise ServiceError(f"Message {uid} has no attachment part {part_id}.")
            piece = match[1]
            size = _estimate_size(piece)
            cap = self.config.attachment_max
            if size > cap:
                raise ServiceError(
                    f"That attachment is {size} bytes, over the {cap} byte cap, so it was not returned."
                )
            data = piece.get_payload(decode=True) or b""
            if len(data) > cap:
                raise ServiceError(
                    f"That attachment is {len(data)} bytes, over the {cap} byte cap, so it was not returned."
                )
            return {
                "id": uid,
                "mailbox": mailbox,
                "part": part_id,
                "filename": piece.get_filename() or "attachment",
                "content_type": piece.get_content_type(),
                "size": len(data),
                "data_base64": base64.b64encode(data).decode("ascii"),
            }

        return self._call(op)

    def _store_flag(self, mailbox: str, uid: str, action: str, flag: str) -> dict:
        mailbox = check_mailbox(mailbox)
        uid = check_uid(uid)

        def op(imap):
            self._select(imap, mailbox, readonly=False)
            typ, _ = imap.uid("STORE", uid, action, flag)
            if typ != "OK":
                raise ServiceError("iCloud did not update that message.")
            return {"id": uid, "mailbox": mailbox, "updated": True}

        return self._call(op)

    def mark_read(self, mailbox: str, uid: str) -> dict:
        return self._store_flag(mailbox, uid, "+FLAGS", "(\\Seen)")

    def mark_unread(self, mailbox: str, uid: str) -> dict:
        return self._store_flag(mailbox, uid, "-FLAGS", "(\\Seen)")

    def flag(self, mailbox: str, uid: str, flagged: bool) -> dict:
        action = "+FLAGS" if flagged else "-FLAGS"
        result = self._store_flag(mailbox, uid, action, "(\\Flagged)")
        result["flagged"] = bool(flagged)
        return result

    def move(self, mailbox: str, uid: str, destination: str) -> dict:
        mailbox = check_mailbox(mailbox)
        destination = check_mailbox(destination)
        uid = check_uid(uid)
        if mailbox == destination:
            raise ServiceError("The message is already in that mailbox.")

        def op(imap):
            caps = self._capabilities(imap)
            self._select(imap, mailbox, readonly=False)
            quoted = quote_mailbox(destination)
            if "MOVE" in caps:
                typ, _ = imap.uid("MOVE", uid, quoted)
                if typ != "OK":
                    raise ServiceError(f"Could not move the message to {destination!r}.")
                return {"id": uid, "from_mailbox": mailbox, "mailbox": destination, "moved": True}
            if "UIDPLUS" not in caps:
                raise ServiceError(
                    "This server cannot move one message without risking other deleted mail. Nothing was changed."
                )
            typ, _ = imap.uid("COPY", uid, quoted)
            if typ != "OK":
                raise ServiceError(f"Could not copy the message to {destination!r}. Nothing was removed.")
            typ, _ = imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            if typ != "OK":
                raise ServiceError("The message was copied, but the original could not be marked. Both copies may exist.")
            typ, _ = imap.uid("EXPUNGE", uid)
            if typ != "OK":
                raise ServiceError("The message was copied, but the original is still there and marked deleted.")
            return {"id": uid, "from_mailbox": mailbox, "mailbox": destination, "moved": True}

        return self._call(op)

    def _capabilities(self, imap) -> set[str]:
        cached = getattr(imap, "_icloud_caps", None)
        if cached is not None:
            return cached
        typ, data = imap.capability()
        blob = b" ".join(part if isinstance(part, bytes) else str(part).encode() for part in (data or []))
        caps = {piece.decode("ascii", "ignore").upper() for piece in blob.split()}
        imap._icloud_caps = caps
        return caps

    def _message_in_sent(self, imap, message_id: str) -> bool:
        self._select(imap, self.config.sent_mailbox, readonly=True)
        typ, data = imap.uid("SEARCH", "HEADER", "Message-ID", quote_imap(message_id))
        if typ != "OK" or not data or not data[0]:
            return False
        raw = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
        return bool(raw.split())

    def _append_sent(self, raw: bytes, message_id: str) -> bool:
        def op(imap):
            if message_id and self._message_in_sent(imap, message_id):
                return True
            typ, _ = imap.append(self.config.sent_mailbox, "(\\Seen)", None, raw)
            if typ != "OK":
                raise ServiceError(f"Could not save a copy in {self.config.sent_mailbox!r}.")
            return True

        return self._call(op)

    def _smtp(self, message: EmailMessage) -> None:
        try:
            smtp = self._smtp_factory(SMTP_HOST, SMTP_PORT, timeout=self.config.timeout)
        except (OSError, TimeoutError) as exc:
            raise ServiceError("Could not reach smtp.mail.me.com. " + redact(str(exc), self.secrets)) from None
        try:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(self.config.user, self.config.credential)
            if message is not None:
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise ServiceError(
                "iCloud rejected the SMTP login. Check ICLOUD_USER and the app-specific password."
            ) from None
        except (OSError, TimeoutError, smtplib.SMTPException) as exc:
            raise ServiceError("Could not send the email. " + redact(str(exc), self.secrets)) from None
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    def check_smtp(self) -> dict:
        try:
            smtp = self._smtp_factory(SMTP_HOST, SMTP_PORT, timeout=self.config.timeout)
        except (OSError, TimeoutError) as exc:
            raise ServiceError("Could not reach smtp.mail.me.com. " + redact(str(exc), self.secrets)) from None
        try:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(self.config.user, self.config.credential)
        except smtplib.SMTPAuthenticationError:
            raise ServiceError("iCloud rejected the SMTP login. Check ICLOUD_USER and the app-specific password.") from None
        except (OSError, TimeoutError, smtplib.SMTPException) as exc:
            raise ServiceError("SMTP login failed. " + redact(str(exc), self.secrets)) from None
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        return {"ok": True}

    def _deliver(self, message: EmailMessage) -> dict:
        self._smtp(message)
        raw = message.as_bytes()
        message_id = message["Message-ID"] or ""
        saved = False
        warning = ""
        try:
            saved = self._append_sent(raw, message_id)
        except ServiceError as exc:
            warning = "The email was sent, but saving a copy to Sent failed: " + str(exc)
        return {
            "sent": True,
            "message_id": message_id,
            "saved_to_sent": saved,
            "warning": warning,
        }

    def send(self, to: str, subject: str, body: str, cc: str | None = None, bcc: str | None = None) -> dict:
        recipients = parse_addresses(to, "To")
        if not recipients:
            raise ServiceError("To is required.")
        copied = parse_addresses(cc, "Cc")
        hidden = parse_addresses(bcc, "Bcc")
        message = EmailMessage()
        message["From"] = self.config.user
        message["To"] = ", ".join(recipients)
        if copied:
            message["Cc"] = ", ".join(copied)
        if hidden:
            message["Bcc"] = ", ".join(hidden)
        message["Subject"] = check_subject(subject)
        message["Date"] = email.utils.format_datetime(datetime.now(AZ))
        message["Message-ID"] = email.utils.make_msgid(domain=self.config.user.split("@", 1)[1])
        message.set_content(check_body(body))
        return self._deliver(message)

    def reply(
        self,
        mailbox: str,
        uid: str,
        body: str,
        to: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        subject: str | None = None,
    ) -> dict:
        original = self.get_message(mailbox, uid)
        target = to if to and str(to).strip() else (original.get("reply_to") or original.get("from") or "")
        recipients = parse_addresses(target, "To")
        if not recipients:
            raise ServiceError("The original message has no reply address. Pass to.")
        message = EmailMessage()
        message["From"] = self.config.user
        message["To"] = ", ".join(recipients)
        copied = parse_addresses(cc, "Cc")
        hidden = parse_addresses(bcc, "Bcc")
        if copied:
            message["Cc"] = ", ".join(copied)
        if hidden:
            message["Bcc"] = ", ".join(hidden)
        message["Subject"] = check_subject(subject) if subject else reply_subject(original.get("subject") or "")
        message["Date"] = email.utils.format_datetime(datetime.now(AZ))
        origin_id = (original.get("message_id") or "").strip()
        if origin_id:
            message["In-Reply-To"] = origin_id
            prior = (original.get("references") or "").split()
            if origin_id not in prior:
                prior.append(origin_id)
            message["References"] = " ".join(prior[-20:])
        message["Message-ID"] = email.utils.make_msgid(domain=self.config.user.split("@", 1)[1])
        message.set_content(check_body(body))
        return self._deliver(message)
