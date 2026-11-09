"""Dates, HTML, iCalendar, vCard, bearer auth, and config."""

import os
import unittest
from datetime import datetime, timezone

from icloud_mcp.auth import bearer_matches, host_allowed
from icloud_mcp.config import Config, ConfigError
from icloud_mcp.dates import AZ, parse_az_bound, parse_icloud_tz, show_az
from icloud_mcp.htmltext import html_to_text
from icloud_mcp.ical import Component, Prop, parse_ics, parse_when, serialize, show_when, wrap_calendar, write_when
from icloud_mcp.mail import build_search_criteria, decode_mutf7, quote_imap
from fakes import fixture_credential
from icloud_mcp.vcard import Card, parse_vcard, serialize_vcard


class DatesTest(unittest.TestCase):
    def test_phoenix_has_no_dst(self):
        winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
        self.assertEqual(show_az(winter), "2026-01-15 05:00 AZ")
        self.assertEqual(show_az(summer), "2026-07-15 05:00 AZ")
        self.assertEqual(winter.astimezone(AZ).strftime("%z"), "-0700")
        self.assertEqual(summer.astimezone(AZ).strftime("%z"), "-0700")

    def test_date_bounds_are_arizona_midnights(self):
        start = parse_az_bound("2026-11-08", end=False)
        end = parse_az_bound("2026-11-08", end=True)
        self.assertEqual(start.isoformat(), "2026-11-08T00:00:00-07:00")
        self.assertEqual(end.isoformat(), "2026-11-09T00:00:00-07:00")


class HtmlTest(unittest.TestCase):
    def test_strips_script_and_breaks_lines(self):
        text = html_to_text("<p>Hello&nbsp;there</p><script>alert(1)</script><div>Next<br>line</div>")
        self.assertNotIn("alert", text)
        self.assertIn("Hello there", text)
        self.assertIn("Next", text)
        self.assertIn("line", text)


class IcalTest(unittest.TestCase):
    def test_round_trip_escapes_and_utc_z(self):
        event = Component("VEVENT")
        event.set("UID", "abc")
        event.set("SUMMARY", "Cafe, lunch; 1")
        token, params = write_when(datetime(2026, 11, 8, 9, tzinfo=AZ), False)
        event.set("DTSTART", token, params)
        text = serialize(wrap_calendar(event))
        self.assertIn("DTSTART:20261108T160000Z", text)
        self.assertNotIn("BEGIN:VTIMEZONE", text)
        parsed = parse_ics(text)
        summary = [child for child in parsed.children if child.name == "VEVENT"][0].text("SUMMARY")
        self.assertEqual(summary, "Cafe, lunch; 1")


class WhenParseTest(unittest.TestCase):
    def test_utc_z_is_fifteen_digits_then_arizona(self):
        value, all_day = parse_when(Prop("DTSTART", {}, "20261107T170000Z"))
        self.assertFalse(all_day)
        self.assertIsInstance(value, datetime)
        self.assertEqual(value.astimezone(timezone.utc).strftime("%H:%M"), "17:00")
        self.assertEqual(show_when(value, False), "2026-11-07 10:00 AZ")

    def test_tzid_stays_in_that_zone(self):
        value, all_day = parse_when(
            Prop("DTSTART", {"TZID": "America/Phoenix"}, "20261108T090000")
        )
        self.assertFalse(all_day)
        self.assertEqual(show_when(value, False), "2026-11-08 09:00 AZ")

    def test_floating_time_is_read_as_arizona(self):
        value, all_day = parse_when(Prop("DTSTART", {}, "20261108T090000"))
        self.assertFalse(all_day)
        self.assertEqual(value.tzinfo, AZ)
        self.assertEqual(show_when(value, False), "2026-11-08 09:00 AZ")

    def test_date_only_is_all_day(self):
        for prop in (
            Prop("DTSTART", {"VALUE": "DATE"}, "20261108"),
            Prop("DTSTART", {}, "20261108"),
        ):
            value, all_day = parse_when(prop)
            self.assertTrue(all_day)
            self.assertEqual(show_when(value, True), "2026-11-08 (all day) AZ")



class UtcWriteDstTest(unittest.TestCase):
    """Timed writes are UTC (Z). Instant is correct in DST and non-DST zones."""

    def test_new_york_summer_and_winter_are_utc_z(self):
        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
        summer = datetime(2026, 7, 15, 9, 0, tzinfo=ny)  # EDT, UTC-4
        winter = datetime(2026, 1, 15, 9, 0, tzinfo=ny)  # EST, UTC-5
        s_tok, s_params = write_when(summer, False)
        w_tok, w_params = write_when(winter, False)
        self.assertEqual(s_params, {})
        self.assertEqual(w_params, {})
        self.assertEqual(s_tok, "20260715T130000Z")
        self.assertEqual(w_tok, "20260115T140000Z")
        s_val, _ = parse_when(Prop("DTSTART", s_params, s_tok))
        w_val, _ = parse_when(Prop("DTSTART", w_params, w_tok))
        self.assertEqual(s_val.astimezone(timezone.utc), summer.astimezone(timezone.utc))
        self.assertEqual(w_val.astimezone(timezone.utc), winter.astimezone(timezone.utc))

    def test_phoenix_summer_is_utc_z_no_dst(self):
        summer = datetime(2026, 7, 15, 9, 0, tzinfo=AZ)
        tok, params = write_when(summer, False)
        self.assertEqual(params, {})
        self.assertEqual(tok, "20260715T160000Z")
        value, all_day = parse_when(Prop("DTSTART", params, tok))
        self.assertFalse(all_day)
        self.assertEqual(show_when(value, False), "2026-07-15 09:00 AZ")

    def test_new_york_as_icloud_tz_display_and_round_trip(self):
        import importlib

        import icloud_mcp.dates as dates_mod
        import icloud_mcp.ical as ical_mod

        os.environ["ICLOUD_TZ"] = "America/New_York"
        self.addCleanup(os.environ.pop, "ICLOUD_TZ", None)
        importlib.reload(dates_mod)
        importlib.reload(ical_mod)
        try:
            self.assertEqual(dates_mod.TZ_NAME, "America/New_York")
            winter_utc = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)  # 12:00 EST
            summer_utc = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)  # 12:00 EDT
            self.assertEqual(dates_mod.show_az(winter_utc), "2026-01-15 12:00 EST")
            self.assertEqual(dates_mod.show_az(summer_utc), "2026-07-15 12:00 EDT")
            start = dates_mod.parse_az_bound("2026-07-15T09:00", end=False)
            tok, params = ical_mod.write_when(start, False)
            self.assertEqual(tok, "20260715T130000Z")
            val, _ = ical_mod.parse_when(Prop("DTSTART", params, tok))
            self.assertEqual(ical_mod.show_when(val, False), "2026-07-15 09:00 EDT")
            jan = dates_mod.parse_az_bound("2026-01-15T09:00", end=False)
            jtok, jparams = ical_mod.write_when(jan, False)
            self.assertEqual(jtok, "20260115T140000Z")
            jval, _ = ical_mod.parse_when(Prop("DTSTART", jparams, jtok))
            self.assertEqual(ical_mod.show_when(jval, False), "2026-01-15 09:00 EST")
        finally:
            os.environ.pop("ICLOUD_TZ", None)
            importlib.reload(dates_mod)
            importlib.reload(ical_mod)


class VcardTest(unittest.TestCase):
    def test_round_trip(self):
        card = Card()
        card.set_single("FN", "Jane Doe")
        card.set_single("NOTE", "line\n2")
        parsed = parse_vcard(serialize_vcard(card))
        self.assertEqual(parsed.text("FN"), "Jane Doe")
        self.assertEqual(parsed.text("NOTE"), "line\n2")


class AuthTest(unittest.TestCase):
    def test_bearer_and_host(self):
        token = "bearer-token-value-ok"
        self.assertTrue(bearer_matches("Bearer " + token, token))
        self.assertTrue(bearer_matches("bearer " + token, token))
        self.assertFalse(bearer_matches("Bearer " + token + "x", token))
        self.assertFalse(bearer_matches("Bearer shorter", token))
        self.assertFalse(bearer_matches("", token))
        self.assertFalse(bearer_matches("Basic " + token, token))
        self.assertTrue(host_allowed("your-name.duckdns.org", "your-name.duckdns.org"))
        self.assertTrue(host_allowed("127.0.0.1:8092", "your-name.duckdns.org"))
        self.assertFalse(host_allowed("evil.example", "your-name.duckdns.org"))


class ConfigTest(unittest.TestCase):
    def test_rejects_a_short_bearer(self):
        os.environ["ICLOUD_USER"] = "you@icloud.com"
        os.environ["ICLOUD_APP_PASSWORD"] = fixture_credential()
        os.environ["MCP_BEARER_TOKEN"] = "too-short"
        with self.assertRaises(ConfigError):
            Config.from_env()
        os.environ["MCP_BEARER_TOKEN"] = "bearer-token-value-ok"
        config = Config.from_env()
        self.assertEqual(config.user, "you@icloud.com")
        self.assertEqual(config.public_host, "your-name.duckdns.org")

    def test_rejects_a_bad_icloud_tz(self):
        os.environ["ICLOUD_USER"] = "you@icloud.com"
        os.environ["ICLOUD_APP_PASSWORD"] = fixture_credential()
        os.environ["MCP_BEARER_TOKEN"] = "bearer-token-value-ok"
        os.environ["ICLOUD_TZ"] = "Not/ARealZone"
        with self.assertRaises(ConfigError) as caught:
            Config.from_env()
        self.assertIn("ICLOUD_TZ", str(caught.exception))
        self.assertIn("Not/ARealZone", str(caught.exception))
        with self.assertRaises(ConfigError) as caught2:
            parse_icloud_tz("Not/ARealZone")
        self.assertIn("known IANA", str(caught2.exception))

    def tearDown(self):
        for name in ("ICLOUD_USER", "ICLOUD_APP_PASSWORD", "MCP_BEARER_TOKEN", "MCP_PUBLIC_HOST", "ICLOUD_TZ"):
            os.environ.pop(name, None)


class SearchQuoteTest(unittest.TestCase):
    def test_quotes_injection(self):
        criteria = build_search_criteria(
            sender='a" OR UNSEEN',
            to=None,
            subject=None,
            text=None,
            since="2026-11-01",
            before="2026-11-08",
            unread=True,
        )
        self.assertIn('FROM "a\\" OR UNSEEN"', criteria)
        self.assertIn("SINCE 01-Nov-2026", criteria)
        self.assertIn("BEFORE 08-Nov-2026", criteria)
        self.assertTrue(criteria.startswith("UNSEEN"))

    def test_mutf7(self):
        encoded = "Café".encode("utf-7").decode("ascii").replace("+", "&").replace("/", ",")
        self.assertEqual(decode_mutf7(encoded), "Café")
        self.assertEqual(decode_mutf7("A &- B"), "A & B")
        self.assertEqual(quote_imap("plain"), '"plain"')


if __name__ == "__main__":
    unittest.main()
