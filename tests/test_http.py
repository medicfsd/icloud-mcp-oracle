"""Tool names, approval wording, and the bearer gate in front of Streamable HTTP."""

import asyncio
import json
import unittest

from icloud_mcp.app import create_app
from icloud_mcp.config import Config
from icloud_mcp.dav import DavClient
from icloud_mcp.mail import MailClient
from icloud_mcp.tools import INSTRUCTIONS, build_server
from fakes import FakeCloud, FakeIMAP, FakeSMTP, fixture_credential

TOKEN = "bearer-token-value-ok"

EXPECTED = {
    "mail_list_mailboxes",
    "mail_search_messages",
    "mail_get_message",
    "mail_get_attachment",
    "mail_mark_read",
    "mail_mark_unread",
    "mail_move_message",
    "mail_flag",
    "mail_send_message",
    "mail_reply_to_message",
    "calendar_list_calendars",
    "calendar_list_events",
    "calendar_get_event",
    "calendar_create_event",
    "calendar_update_event",
    "calendar_delete_event",
    "contacts_search",
    "contacts_get",
    "contacts_create",
    "contacts_update",
    "reminders_list_lists",
    "reminders_list",
    "reminders_add",
    "reminders_complete",
    "reminders_edit",
}


def config():
    return Config(
        user="you@icloud.com",
        credential=fixture_credential(),
        bearer=TOKEN,
        public_host="your-name.duckdns.org",
        sent_mailbox="Sent Messages",
        port=8092,
        timeout=5,
        attachment_max=1000,
    )


def clients():
    store = {"INBOX": {}, "Sent Messages": {}}
    mail = MailClient(config(), connector=lambda: FakeIMAP(store), smtp_factory=FakeSMTP([], fixture_credential()))
    dav = DavClient(
        "you@icloud.com",
        fixture_credential(),
        transport=FakeCloud("you@icloud.com", fixture_credential()),
    )
    return mail, dav


class ToolCatalogTest(unittest.TestCase):
    def test_names_descriptions_and_a_read(self):
        mail, dav = clients()
        mcp = build_server(mail, dav)

        async def run():
            tools = await mcp.list_tools()
            by_name = {tool.name: tool for tool in tools}
            self.assertEqual(set(by_name), EXPECTED)
            self.assertIn("America/Phoenix", INSTRUCTIONS)
            self.assertIn("sends a real email", by_name["mail_send_message"].description.lower())
            self.assertIn("sends a real email", by_name["mail_reply_to_message"].description.lower())
            self.assertIn("permanently delete", by_name["calendar_delete_event"].description.lower())
            self.assertTrue(by_name["calendar_delete_event"].annotations.destructive_hint)
            self.assertFalse(by_name["mail_send_message"].annotations.read_only_hint)
            self.assertTrue(by_name["calendar_list_events"].annotations.read_only_hint)
            self.assertIn("Work Calendar", by_name["calendar_list_events"].description)
            deletes = [name for name in by_name if "delete" in name]
            self.assertEqual(deletes, ["calendar_delete_event"])
            result = await mcp.call_tool("mail_list_mailboxes", {})
            self.assertFalse(result.is_error)
            self.assertIn("INBOX", json.dumps(result.structured_content))

        asyncio.run(run())


class HttpAuthTest(unittest.TestCase):
    def test_missing_and_wrong_bearer_are_rejected(self):
        app = create_app(config(), *clients())

        async def request(headers):
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message):
                messages.append(message)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/mcp/icloud",
                "raw_path": b"/mcp/icloud",
                "query_string": b"",
                "headers": [[key.encode(), value.encode()] for key, value in headers.items()],
                "client": ("203.0.113.4", 9),
                "server": ("127.0.0.1", 8092),
            }
            await app(scope, receive, send)
            status = next(message["status"] for message in messages if message["type"] == "http.response.start")
            header_map = {
                key.decode().lower(): value.decode()
                for message in messages
                if message["type"] == "http.response.start"
                for key, value in message["headers"]
            }
            body = b"".join(
                message.get("body", b"") for message in messages if message["type"] == "http.response.body"
            )
            return status, header_map, body

        async def run():
            status, headers, body = await request({"host": "your-name.duckdns.org", "content-type": "application/json"})
            self.assertEqual(status, 401)
            self.assertIn("Bearer", headers.get("www-authenticate", ""))
            self.assertNotIn(b"resource_metadata", body)
            self.assertNotIn(TOKEN.encode(), body)
            status, _headers, body = await request(
                {
                    "host": "your-name.duckdns.org",
                    "authorization": "Bearer not-the-token-value",
                    "content-type": "application/json",
                }
            )
            self.assertEqual(status, 401)
            self.assertNotIn(b"not-the-token-value", body)
            status, _headers, _body = await request(
                {
                    "host": "evil.example",
                    "authorization": "Bearer " + TOKEN,
                    "content-type": "application/json",
                }
            )
            self.assertEqual(status, 421)

        asyncio.run(run())

    def test_initialize_with_the_bearer(self):
        app = create_app(config(), *clients())
        payload = json.dumps(
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

        async def run():
            started = asyncio.Event()
            release = asyncio.Event()

            async def lifespan_receive():
                if not started.is_set():
                    started.set()
                    return {"type": "lifespan.startup"}
                await release.wait()
                return {"type": "lifespan.shutdown"}

            ready = asyncio.Event()

            async def lifespan_send(message):
                if message["type"] == "lifespan.startup.complete":
                    ready.set()
                if message["type"] == "lifespan.startup.failed":
                    raise RuntimeError(message.get("message"))

            task = asyncio.create_task(
                app({"type": "lifespan", "asgi": {"version": "3.0"}}, lifespan_receive, lifespan_send)
            )
            await ready.wait()
            messages = []
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                await asyncio.sleep(0.05)
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)

            headers = {
                "host": "127.0.0.1:8092",
                "authorization": "Bearer " + TOKEN,
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            }
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/mcp/icloud",
                "raw_path": b"/mcp/icloud",
                "query_string": b"",
                "headers": [[key.encode(), value.encode()] for key, value in headers.items()],
                "client": ("127.0.0.1", 9),
                "server": ("127.0.0.1", 8092),
            }
            await app(scope, receive, send)
            status = next(message["status"] for message in messages if message["type"] == "http.response.start")
            body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
            release.set()
            await task
            self.assertEqual(status, 200, body.decode("utf-8", "replace")[:500])
            self.assertIn(b"icloud", body)
            self.assertNotIn(fixture_credential().encode(), body)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
