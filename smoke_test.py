#!/usr/bin/env python3
"""Check iCloud login and the local MCP listener.

Does not send mail, and does not create, change, or delete anything.
Prints mailbox names, calendar names, and reminder-list names so you can see
that calendars are visible. It does not print message
subjects, contact names, the app-specific password, or the bearer token.

    sudo /opt/icloud-mcp/venv/bin/python /opt/icloud-mcp/smoke_test.py
    sudo /opt/icloud-mcp/venv/bin/python /opt/icloud-mcp/smoke_test.py /etc/icloud-mcp.env
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from icloud_mcp.config import PATH, Config
from icloud_mcp.dav import DavClient
from icloud_mcp.errors import ServiceError
from icloud_mcp.mail import MailClient


def load_env(path: str) -> None:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def step(label: str, fn):
    try:
        return fn()
    except ServiceError as exc:
        print(f"{label}: FAILED: {exc}")
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"{label}: FAILED ({type(exc).__name__})")
        raise SystemExit(1) from None


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/etc/icloud-mcp.env"
    if not os.path.isfile(path):
        raise SystemExit(f"Env file not found: {path}")
    load_env(path)
    try:
        config = Config.from_env()
    except Exception as exc:
        raise SystemExit(str(exc)) from None
    print(f"config ok for {config.user}")

    mail = MailClient(config)
    boxes = step("mail", mail.list_mailboxes)
    print("mailboxes:", ", ".join(item["name"] for item in boxes["mailboxes"]))
    step("smtp", mail.check_smtp)
    print("smtp login ok")

    dav = DavClient(config.user, config.credential, timeout=config.timeout)
    calendars = step("calendar", dav.list_calendars)
    event_names = [item["name"] for item in calendars if "VEVENT" in item["components"] or not item["components"]]
    todo_names = [item["name"] for item in calendars if "VTODO" in item["components"]]
    print("calendars:", ", ".join(event_names) or "(none)")
    print("reminder lists:", ", ".join(todo_names) or "(none)")
    print(
        "Reminder lists upgraded in the Reminders app may be missing. "
        "A list that only has Apple's placeholder text is not the real list."
    )
    books = step("contacts", dav.list_addressbooks)
    print(f"address books: {len(books)}")

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        }
    ).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{config.port}{PATH}",
        data=body,
        headers={
            "Authorization": "Bearer " + config.bearer,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            payload = response.read(8000)
            status = response.status
    except urllib.error.HTTPError as exc:
        print(f"mcp http: FAILED HTTP {exc.code}")
        raise SystemExit(1) from None
    except urllib.error.URLError as exc:
        print(f"mcp http: FAILED ({exc.reason}). Is icloud-mcp.service running?")
        raise SystemExit(1) from None
    if status != 200 or b"icloud" not in payload:
        print("mcp http: FAILED initialize")
        raise SystemExit(1)
    print(f"mcp http ok at http://127.0.0.1:{config.port}{PATH}")


if __name__ == "__main__":
    main()
