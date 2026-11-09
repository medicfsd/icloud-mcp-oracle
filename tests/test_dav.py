"""Calendar, contacts, and reminders against a fake CalDAV/CardDAV server."""

import unittest
from datetime import datetime

from icloud_mcp.calendar import CalendarService
from icloud_mcp.ical import Component, serialize, wrap_calendar
from icloud_mcp.contacts import ContactsService
from icloud_mcp.dav import DavClient
from icloud_mcp.dates import AZ
from icloud_mcp.errors import ServiceError
from icloud_mcp.reminders import UPGRADED_NOTE, RemindersService
from fakes import FakeCloud, fixture_credential

USER = "you@icloud.com"


def services(cloud: FakeCloud | None = None):
    cloud = cloud or FakeCloud(USER, fixture_credential())
    cloud.add_event(
        "work",
        "shift-1",
        "Morning shift",
        datetime(2026, 11, 8, 9, tzinfo=AZ),
        datetime(2026, 11, 8, 10, tzinfo=AZ),
    )
    cloud.add_todo("personal", "milk-1", "Buy milk")
    cloud.add_contact("jane-1", "Jane Doe", "jane@example.com", "555-0100")
    dav = DavClient(USER, fixture_credential(), timeout=5, transport=cloud)
    return cloud, CalendarService(dav), ContactsService(dav), RemindersService(dav)


class CalendarTest(unittest.TestCase):
    def test_named_calendar_is_easy_to_read(self):
        cloud, calendar, _contacts, _reminders = services()
        names = [item["name"] for item in calendar.list_calendars()["calendars"]]
        self.assertIn("Work Calendar", names)
        self.assertNotIn("Personal", names)
        found = calendar.list_events("work calendar", "2026-11-08", "2026-11-08")
        self.assertEqual(found["calendar"], "Work Calendar")
        self.assertEqual(found["events"][0]["summary"], "Morning shift")
        self.assertEqual(found["events"][0]["start"], "2026-11-08 09:00 AZ")
        report = [body for method, _url, body in cloud.calls if method == "REPORT"][-1]
        self.assertIn("expand", report)
        empty = calendar.list_events("Work Calendar", "2026-11-09", "2026-11-09", text="shift")
        self.assertEqual(empty["events"], [])

    def test_ambiguous_name(self):
        cloud, calendar, _c, _r = services()
        cloud.calendars["extra"] = {"name": "Work Notes", "components": ["VEVENT"], "items": {}}
        with self.assertRaises(ServiceError) as caught:
            calendar.list_events("Work", "2026-11-08", "2026-11-08")
        self.assertIn("more than one", str(caught.exception))

    def test_create_update_delete(self):
        cloud, calendar, _c, _r = services()
        created = calendar.create_event(
            "Home",
            "Dentist",
            "2026-11-10T14:00",
            "2026-11-10T15:00",
            location="Office",
        )
        stored = cloud.calendars["home"]["items"][created["uid"]]["body"]
        self.assertIn("DTSTART:20261110T210000Z", stored)
        self.assertNotIn("TZID=", stored)
        self.assertIn("SUMMARY:Dentist", stored)
        updated = calendar.update_event("Home", created["uid"], summary="Dentist follow-up")
        self.assertEqual(updated["summary"], "Dentist follow-up")
        holiday = calendar.create_event("Home", "Holiday", "2026-11-11", all_day=True)
        self.assertEqual(holiday["start"], "2026-11-11 (all day) AZ")
        self.assertTrue(holiday["all_day"])
        deleted = calendar.delete_event("Home", created["uid"])
        self.assertTrue(deleted["deleted"])
        self.assertNotIn(created["uid"], cloud.calendars["home"]["items"])
        with self.assertRaises(ServiceError):
            calendar.delete_event("Home", created["uid"])

    def test_redirect_stays_on_icloud_and_evil_host_is_refused(self):
        cloud = FakeCloud(USER, fixture_credential())
        cloud.redirect_once = True
        dav = DavClient(USER, fixture_credential(), transport=cloud)
        home = dav.calendar_home()
        self.assertIn("p21-caldav.icloud.com", home)
        evil = FakeCloud(USER, fixture_credential())
        evil.evil = True
        dav = DavClient(USER, fixture_credential(), transport=evil)
        with self.assertRaises(ServiceError) as caught:
            dav.calendar_home()
        self.assertIn("non-iCloud", str(caught.exception))
        self.assertFalse(any("evil.example" in url for _method, url, _body in evil.calls))

    def test_subscribed_calendar_is_read_from_its_feed(self):
        cloud, calendar, _contacts, reminders = services()
        cloud.calendars.pop("work")
        feed = "webcal://feeds.example/work.ics"
        master = Component("VEVENT")
        master.set("UID", "shift-week")
        master.set("SUMMARY", "Morning shift")
        master.set("RRULE", "FREQ=WEEKLY;BYDAY=MO")
        master.set("DTSTART", "20261102T090000", {"TZID": "America/Phoenix"})
        master.set("DTEND", "20261102T100000", {"TZID": "America/Phoenix"})
        master.set("EXDATE", "20261109T090000", {"TZID": "America/Phoenix"})
        override = Component("VEVENT")
        override.set("UID", "shift-week")
        override.set("SUMMARY", "Cover shift")
        override.set("RECURRENCE-ID", "20261116T090000", {"TZID": "America/Phoenix"})
        override.set("DTSTART", "20261116T090000", {"TZID": "America/Phoenix"})
        override.set("DTEND", "20261116T100000", {"TZID": "America/Phoenix"})
        orientation = Component("VEVENT")
        orientation.set("UID", "orient-1")
        orientation.set("SUMMARY", "Orientation")
        orientation.set("DTSTART", "20261110T130000", {"TZID": "America/Phoenix"})
        orientation.set("DTEND", "20261110T140000", {"TZID": "America/Phoenix"})
        cloud.calendars["sub"] = {
            "name": "Work Calendar",
            "components": ["VEVENT"],
            "subscribed": True,
            "feed": feed,
            "report_fails": True,
            "items": {},
        }
        cloud.feeds["https://feeds.example/work.ics"] = serialize(
            wrap_calendar(master, override, orientation)
        ).encode()
        listed = calendar.list_calendars()["calendars"]
        match = next(item for item in listed if item["name"] == "Work Calendar")
        self.assertTrue(match["subscribed"])
        self.assertTrue(match["read_only"])
        rendered = str(listed)
        self.assertNotIn("webcal", rendered)
        self.assertNotIn("feeds.example", rendered)
        found = calendar.list_events("Work Calendar", "2026-11-08", "2026-11-16")
        summaries = [item["summary"] for item in found["events"]]
        self.assertEqual(summaries, ["Orientation", "Cover shift"])
        self.assertEqual(found["events"][1]["start"], "2026-11-16 09:00 AZ")
        self.assertTrue(any(method == "GET" and url.startswith("https://") for method, url, _body in cloud.calls))
        loaded = calendar.get_event("Work Calendar", "shift-week")
        self.assertEqual(loaded["summary"], "Morning shift")
        self.assertIn("WEEKLY", loaded["rrule"])
        self.assertEqual([item["name"] for item in reminders.list_lists()["lists"]], ["Personal"])
        before = len(cloud.calls)
        for call in (
            lambda: calendar.create_event("Work Calendar", "Nope", "2026-11-12T09:00", "2026-11-12T10:00"),
            lambda: calendar.update_event("Work Calendar", "shift-week", summary="Changed"),
            lambda: calendar.delete_event("Work Calendar", "shift-week"),
        ):
            with self.assertRaises(ServiceError) as caught:
                call()
            self.assertIn("subscribed read-only", str(caught.exception))
        self.assertFalse(any(method in {"PUT", "DELETE"} for method, _url, _body in cloud.calls[before:]))
        cloud.feeds.clear()
        with self.assertRaises(ServiceError) as caught:
            calendar.list_events("Work Calendar", "2026-11-08", "2026-11-16")
        self.assertNotIn("feeds.example", str(caught.exception))
        self.assertNotIn("webcal", str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))

    def test_feed_fallback_keeps_utc_z_events(self):
        cloud, calendar, _contacts, _reminders = services()
        cloud.calendars.pop("work")
        event = Component("VEVENT")
        event.set("UID", "utc-shift")
        event.set("SUMMARY", "Evening shift")
        event.set("DTSTART", "20261107T170000Z")
        event.set("DTEND", "20261107T180000Z")
        cloud.calendars["sub"] = {
            "name": "Work Calendar",
            "components": ["VEVENT"],
            "subscribed": True,
            "feed": "webcal://feeds.example/work.ics",
            "report_fails": True,
            "items": {},
        }
        cloud.feeds["https://feeds.example/work.ics"] = serialize(wrap_calendar(event)).encode()
        found = calendar.list_events("Work Calendar", "2026-11-07", "2026-11-07")
        self.assertEqual(len(found["events"]), 1)
        self.assertEqual(found["events"][0]["summary"], "Evening shift")
        self.assertEqual(found["events"][0]["start"], "2026-11-07 10:00 AZ")
        self.assertEqual(found["events"][0]["end"], "2026-11-07 11:00 AZ")

    def test_subscribed_calendar_uses_caldav_when_the_server_answers(self):
        cloud, calendar, _contacts, _reminders = services()
        cloud.calendars["work"]["subscribed"] = True
        cloud.calendars["work"]["feed"] = "webcal://feeds.example/work.ics"
        cloud.feeds["https://feeds.example/work.ics"] = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        before = [url for method, url, _body in cloud.calls if method == "GET"]
        found = calendar.list_events("Work Calendar", "2026-11-08", "2026-11-08")
        self.assertEqual(found["events"][0]["summary"], "Morning shift")
        after = [url for method, url, _body in cloud.calls if method == "GET"]
        self.assertEqual(after, before)

    def test_bad_password_does_not_echo(self):
        cloud = FakeCloud(USER, "-".join(("other", "value")))
        dav = DavClient(USER, fixture_credential(), transport=cloud)
        with self.assertRaises(ServiceError) as caught:
            dav.calendar_home()
        self.assertNotIn(fixture_credential(), str(caught.exception))


class ContactsTest(unittest.TestCase):
    def test_search_get_create_update(self):
        _cloud, _calendar, contacts, _reminders = services()
        found = contacts.search("jane")
        self.assertEqual(found["contacts"][0]["full_name"], "Jane Doe")
        self.assertNotIn("note", found["contacts"][0])
        full = contacts.get("jane-1")
        self.assertEqual(full["emails"], ["jane@example.com"])
        created = contacts.create("Sam Lee", email="sam@example.com", phone="555-0199")
        self.assertTrue(created["created"])
        updated = contacts.update(created["uid"], phone="555-0177")
        self.assertEqual(updated["phones"], ["555-0177"])
        self.assertEqual(updated["emails"], ["sam@example.com"])
        self.assertEqual(contacts.search("nobody")["contacts"], [])

    def test_anyof_fallback_still_finds_a_contact(self):
        cloud, _calendar, contacts, _reminders = services()
        cloud.reject_anyof = True
        found = contacts.search("Jane")
        self.assertEqual(found["contacts"][0]["uid"], "jane-1")

    def test_update_keeps_a_photo_property(self):
        cloud, _calendar, contacts, _reminders = services()
        cloud.book["items"]["jane-1"]["body"] = cloud.book["items"]["jane-1"]["body"].replace(
            "END:VCARD", "PHOTO;ENCODING=b;TYPE=JPEG:abc\r\nEND:VCARD"
        )
        contacts.update("jane-1", organization="North Clinic")
        self.assertIn("PHOTO", cloud.book["items"]["jane-1"]["body"])
        self.assertIn("North Clinic", cloud.book["items"]["jane-1"]["body"])


class RemindersTest(unittest.TestCase):
    def test_list_add_complete_edit(self):
        _cloud, _calendar, _contacts, reminders = services()
        lists = [item["name"] for item in reminders.list_lists()["lists"]]
        self.assertEqual(lists, ["Personal"])
        rows = reminders.list_reminders("Personal")
        self.assertEqual(rows["reminders"][0]["title"], "Buy milk")
        added = reminders.add("Personal", "Call office", due="2026-11-12", priority="high")
        self.assertEqual(added["due"], "2026-11-12 (all day) AZ")
        self.assertEqual(added["priority"], "high")
        done = reminders.complete("Personal", added["uid"])
        self.assertTrue(done["completed"])
        hidden = reminders.list_reminders("Personal")
        self.assertTrue(all(item["uid"] != added["uid"] for item in hidden["reminders"]))
        shown = reminders.list_reminders("Personal", include_completed=True)
        match = [item for item in shown["reminders"] if item["uid"] == added["uid"]][0]
        self.assertTrue(match["completed"])
        edited = reminders.edit("Personal", "milk-1", title="Buy oat milk")
        self.assertEqual(edited["title"], "Buy oat milk")

    def test_upgraded_placeholder_is_not_a_real_reminder(self):
        cloud, _calendar, _contacts, reminders = services()
        cloud.calendars["old"] = {"name": "Old List", "components": ["VTODO"], "items": {}}
        cloud.add_todo("old", "ph", "Where are my reminders?", "The creator of this list has upgraded these reminders.")
        result = reminders.list_reminders("Old List")
        self.assertEqual(result["reminders"], [])
        self.assertEqual(result["hidden_placeholders"], 1)
        self.assertEqual(result["warning"], UPGRADED_NOTE)


if __name__ == "__main__":
    unittest.main()
