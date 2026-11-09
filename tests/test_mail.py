"""Mail tools against a fake IMAP and SMTP connection."""

import imaplib
import unittest
from email import message_from_bytes

from icloud_mcp.config import Config
from icloud_mcp.errors import ServiceError
from icloud_mcp.mail import MailClient
from fakes import FakeIMAP, FakeSMTP, fixture_credential, raw_message


def config(**kwargs):
    values = dict(
        user="you@icloud.com",
        credential=fixture_credential(),
        bearer="bearer-token-value-ok",
        public_host="your-name.duckdns.org",
        sent_mailbox="Sent Messages",
        port=8092,
        timeout=5,
        attachment_max=1000,
    )
    values.update(kwargs)
    return Config(**values)


def sample_store():
    return {
        "INBOX": {
            10: {
                "raw": raw_message(subject="Shift notes", body="Bring the binder.", message_id="<one@example.com>"),
                "internal": "07-Nov-2026 09:00:00 -0700",
                "flags": set(),
            },
            11: {
                "raw": raw_message(
                    subject="Picture",
                    sender="Bea <bea@example.com>",
                    body=None,
                    html="<p>See <b>attached</b></p><script>nope()</script>",
                    attachment=("note.txt", b"hello attachment body", "text/plain"),
                    message_id="<two@example.com>",
                ),
                "internal": "07-Nov-2026 11:00:00 -0700",
                "flags": {"\\Seen"},
            },
        },
        "Archive": {},
        "Sent Messages": {},
    }


class MailTest(unittest.TestCase):
    def setUp(self):
        self.store = sample_store()
        self.smtp_sink = []
        self.imap = FakeIMAP(self.store)
        self.client = MailClient(config(), connector=lambda: self.imap, smtp_factory=FakeSMTP(self.smtp_sink, fixture_credential()))

    def test_search_returns_snippet_and_arizona_date(self):
        found = self.client.search(sender="Ada", limit=10)
        self.assertEqual(found["count"], 1)
        row = found["messages"][0]
        self.assertEqual(row["id"], "10")
        self.assertEqual(row["subject"], "Shift notes")
        self.assertIn("Ada", row["from"])
        self.assertIn("binder", row["snippet"])
        self.assertNotIn("Content-Transfer-Encoding", row["snippet"])
        self.assertNotIn("--", row["snippet"])
        self.assertTrue(row["date"].endswith("AZ"))
        criteria = " ".join(str(part) for part in self.imap.searches[-1])
        self.assertIn("FROM", criteria)

    def test_search_snippet_uses_decoded_part_not_mime_headers(self):
        from email.message import EmailMessage

        plain = EmailMessage()
        plain["From"] = "Cara <cara@example.com>"
        plain["To"] = "you@icloud.com"
        plain["Subject"] = "Mime note"
        plain["Date"] = "Sat, 07 Nov 2026 12:00:00 -0700"
        plain["Message-ID"] = "<mime@example.com>"
        plain.set_content("Shift starts at nine.")
        plain.add_alternative("<p>Shift starts at <b>nine</b>.</p><script>nope()</script>", subtype="html")
        self.store["INBOX"][12] = {
            "raw": plain.as_bytes(),
            "internal": "07-Nov-2026 12:00:00 -0700",
            "flags": set(),
        }
        html_only = EmailMessage()
        html_only["From"] = "Dee <dee@example.com>"
        html_only["To"] = "you@icloud.com"
        html_only["Subject"] = "Html only"
        html_only["Date"] = "Sat, 07 Nov 2026 13:00:00 -0700"
        html_only["Message-ID"] = "<html@example.com>"
        html_only.set_content("<p>See the <b>roster</b>.</p><script>nope()</script>", subtype="html")
        self.store["INBOX"][13] = {
            "raw": html_only.as_bytes(),
            "internal": "07-Nov-2026 13:00:00 -0700",
            "flags": set(),
        }
        rows = self.client.search(limit=10)["messages"]
        mime = next(row for row in rows if row["subject"] == "Mime note")
        self.assertIn("Shift starts at nine", mime["snippet"])
        self.assertNotIn("Content-Transfer-Encoding", mime["snippet"])
        self.assertNotIn("Content-Type", mime["snippet"])
        self.assertNotRegex(mime["snippet"], r"^--")
        html = next(row for row in rows if row["subject"] == "Html only")
        self.assertIn("roster", html["snippet"])
        self.assertNotIn("nope", html["snippet"])
        self.assertNotIn("<b>", html["snippet"])
        self.assertNotIn("Content-Transfer-Encoding", html["snippet"])

    def test_get_converts_html_and_lists_attachment(self):
        message = self.client.get_message("INBOX", "11")
        self.assertIn("attached", message["body"])
        self.assertNotIn("nope", message["body"])
        self.assertEqual(message["attachments"][0]["filename"], "note.txt")
        part = message["attachments"][0]["part"]
        fetched = self.client.get_attachment("INBOX", "11", part)
        self.assertIn("aGVsbG8", fetched["data_base64"])

    def test_attachment_over_cap_returns_no_data(self):
        self.client = MailClient(
            config(attachment_max=8),
            connector=lambda: self.imap,
            smtp_factory=FakeSMTP(self.smtp_sink, fixture_credential()),
        )
        with self.assertRaises(ServiceError) as caught:
            self.client.get_attachment("INBOX", "11", "2")
        self.assertNotIn("aGVsbG8", str(caught.exception))
        self.assertIn("cap", str(caught.exception))

    def test_flags_and_move(self):
        self.client.mark_read("INBOX", "10")
        self.assertIn("\\Seen", self.store["INBOX"][10]["flags"])
        self.client.flag("INBOX", "10", True)
        self.assertIn("\\Flagged", self.store["INBOX"][10]["flags"])
        self.client.move("INBOX", "10", "Archive")
        self.assertNotIn(10, self.store["INBOX"])
        self.assertIn(10, self.store["Archive"])

    def test_move_without_move_uses_uidplus(self):
        self.imap.caps = {"IMAP4REV1", "UIDPLUS"}
        self.client.move("INBOX", "10", "Archive")
        self.assertIn(10, self.store["Archive"])
        self.assertNotIn(10, self.store["INBOX"])

    def test_move_without_safe_capability_changes_nothing(self):
        self.imap.caps = {"IMAP4REV1"}
        with self.assertRaises(ServiceError):
            self.client.move("INBOX", "10", "Archive")
        self.assertIn(10, self.store["INBOX"])

    def test_send_and_reply_thread_and_save_sent(self):
        sent = self.client.send("sam@example.com", "Hello", "Body text", cc="cc@example.com")
        self.assertTrue(sent["sent"])
        self.assertEqual(len(self.smtp_sink), 1)
        raw = self.smtp_sink[0]
        self.assertEqual(raw["To"], "sam@example.com")
        self.assertTrue(sent["saved_to_sent"])
        self.assertEqual(len(self.store["Sent Messages"]), 1)
        reply = self.client.reply("INBOX", "10", "On it")
        self.assertEqual(len(self.smtp_sink), 2)
        answered = self.smtp_sink[1]
        self.assertEqual(answered["In-Reply-To"], "<one@example.com>")
        self.assertIn("<one@example.com>", answered["References"])
        self.assertTrue(answered["Subject"].lower().startswith("re:"))
        self.assertTrue(reply["saved_to_sent"])

    def test_rejects_bad_uid_and_redacts_password(self):
        with self.assertRaises(ServiceError):
            self.client.get_message("INBOX", "10 OR 1")

        def explode():
            raise imaplib.IMAP4.error("failure " + fixture_credential())

        client = MailClient(config(), connector=explode, smtp_factory=FakeSMTP(self.smtp_sink, fixture_credential()))
        with self.assertRaises(ServiceError) as caught:
            client.list_mailboxes()
        self.assertNotIn(fixture_credential(), str(caught.exception))

    def test_reconnects_after_the_server_drops(self):
        calls = {"n": 0}

        def connect():
            calls["n"] += 1
            return FakeIMAP(self.store, fail_noop=1 if calls["n"] == 1 else 0)

        client = MailClient(config(), connector=connect, smtp_factory=FakeSMTP(self.smtp_sink, fixture_credential()))
        self.assertEqual(client.list_mailboxes()["mailboxes"][0]["name"] != "", True)
        self.assertGreaterEqual(client.search(limit=1)["count"], 1)
        self.assertEqual(calls["n"], 2)


class HeaderBytesTest(unittest.TestCase):
    def test_message_round_trip_has_subject(self):
        parsed = message_from_bytes(raw_message())
        self.assertEqual(parsed["Subject"], "Hello")


if __name__ == "__main__":
    unittest.main()
