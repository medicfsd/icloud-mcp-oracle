"""MCP tool registration. Names are prefixed so a client can tell the apps apart.

mail_send_message and mail_reply_to_message are separate tools whose
descriptions say they send real email, so an approval rule can catch them.
calendar_delete_event is the only delete tool.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from icloud_mcp.calendar import CalendarService
from icloud_mcp.dates import TZ_NAME, tz_label
from icloud_mcp.contacts import ContactsService
from icloud_mcp.dav import DavClient
from icloud_mcp.errors import ServiceError, redact
from icloud_mcp.mail import MailClient
from icloud_mcp.reminders import RemindersService

log = logging.getLogger("icloud_mcp")

INSTRUCTIONS = f"""This connector is the owner's iCloud account on a self-hosted server.
Times you show or ask for are {TZ_NAME}. Label them {tz_label()}.
Mail, calendar, contacts, and reminders are separate tool groups. There is no delete tool for mail, contacts, or reminders.
mail_send_message and mail_reply_to_message send real email. Do not call either one unless the owner has approved that exact message.
To read a calendar, call calendar_list_events with that calendar's display name.
calendar_delete_event permanently deletes an event, including a whole repeating series.
Reminders come from CalDAV. Lists upgraded in the Reminders app may be missing, or reminders_list may warn that only placeholders came back. Do not invent the missing items.
Notes, iCloud Drive, Photos, and Find My are not on this server.
"""

SEND = (
    "Sends a real email from the owner's iCloud account. This delivers the message to the recipients "
    "and saves a copy in Sent. Do not call it unless the owner has approved this exact email."
)
REPLY = (
    "Sends a real email reply from the owner's iCloud account. This delivers the message to the recipients, "
    "sets In-Reply-To and References so it threads, and saves a copy in Sent. "
    "Do not call it unless the owner has approved this exact reply."
)
DELETE_EVENT = (
    "Permanently delete one event from an iCloud calendar. This removes the event from every device "
    "signed in to the account. A repeating event is removed entirely, not just one occurrence."
)


def _run(service: Any, fn):
    try:
        return fn()
    except ServiceError as exc:
        raise ToolError(str(exc)) from None
    except ToolError:
        raise
    except Exception as exc:
        log.error("icloud tool error: %s", redact(f"{type(exc).__name__}: {exc}", getattr(service, "secrets", [])))
        raise ToolError("The iCloud request failed. The server log has the details.") from None


def _read():
    return ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)


def _write(*, destroy: bool = False, idempotent: bool = False):
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=destroy,
        idempotent_hint=idempotent,
        open_world_hint=True,
    )


def register(mcp: MCPServer, mail: MailClient, dav: DavClient) -> None:
    calendars = CalendarService(dav)
    contacts = ContactsService(dav)
    reminders = RemindersService(dav)

    @mcp.tool(annotations=_read())
    def mail_list_mailboxes() -> dict[str, Any]:
        """List mailbox names in the owner's iCloud mail."""
        return _run(mail, mail.list_mailboxes)

    @mcp.tool(annotations=_read())
    def mail_search_messages(
        mailbox: Annotated[str, Field(description="Mailbox name. Defaults to INBOX.")] = "INBOX",
        sender: Annotated[
            str | None,
            Field(description="From address filter (IMAP FROM).", validation_alias="from"),
        ] = None,
        to: Annotated[str | None, Field(description="To address filter.")] = None,
        subject: Annotated[str | None, Field(description="Subject filter.")] = None,
        text: Annotated[str | None, Field(description="Text found in the headers or body.")] = None,
        since: Annotated[str | None, Field(description=f"Inclusive {TZ_NAME} date YYYY-MM-DD. On that day or later.")] = None,
        before: Annotated[str | None, Field(description=f"Exclusive {TZ_NAME} date YYYY-MM-DD. Earlier than that day.")] = None,
        unread_only: Annotated[bool, Field(description="When true, only unread messages.")] = False,
        limit: Annotated[int, Field(description="How many messages to return.", ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        """Search one mailbox. Returns ids, local-zone dates, From, subject, and a short snippet."""
        return _run(
            mail,
            lambda: mail.search(
                mailbox=mailbox,
                sender=sender,
                to=to,
                subject=subject,
                text=text,
                since=since,
                before=before,
                unread=unread_only,
                limit=limit,
            ),
        )

    @mcp.tool(annotations=_read())
    def mail_get_message(
        mailbox: Annotated[str, Field(description="Mailbox name.")] = "INBOX",
        message_id: Annotated[str, Field(description="Numeric id from mail_search_messages.")] = ...,
    ) -> dict[str, Any]:
        """Read one message: headers, plain text (HTML converted to text), and the attachment list."""
        return _run(mail, lambda: mail.get_message(mailbox, message_id))

    @mcp.tool(annotations=_read())
    def mail_get_attachment(
        mailbox: Annotated[str, Field(description="Mailbox name.")] = "INBOX",
        message_id: Annotated[str, Field(description="Numeric id from mail_search_messages.")] = ...,
        part: Annotated[str, Field(description="Attachment part id from mail_get_message, such as 2.")] = ...,
    ) -> dict[str, Any]:
        """Return one attachment as base64. Over the size cap, this returns an error and no data."""
        return _run(mail, lambda: mail.get_attachment(mailbox, message_id, part))

    @mcp.tool(annotations=_write(idempotent=True))
    def mail_mark_read(
        mailbox: Annotated[str, Field(description="Mailbox name.")] = "INBOX",
        message_id: Annotated[str, Field(description="Numeric id from mail_search_messages.")] = ...,
    ) -> dict[str, Any]:
        """Mark one message read."""
        return _run(mail, lambda: mail.mark_read(mailbox, message_id))

    @mcp.tool(annotations=_write(idempotent=True))
    def mail_mark_unread(
        mailbox: Annotated[str, Field(description="Mailbox name.")] = "INBOX",
        message_id: Annotated[str, Field(description="Numeric id from mail_search_messages.")] = ...,
    ) -> dict[str, Any]:
        """Mark one message unread."""
        return _run(mail, lambda: mail.mark_unread(mailbox, message_id))

    @mcp.tool(annotations=_write())
    def mail_move_message(
        mailbox: Annotated[str, Field(description="Mailbox the message is in now.")] = "INBOX",
        message_id: Annotated[str, Field(description="Numeric id from mail_search_messages.")] = ...,
        destination: Annotated[str, Field(description="Destination mailbox name.")] = ...,
    ) -> dict[str, Any]:
        """Move one message to another mailbox. This does not delete it."""
        return _run(mail, lambda: mail.move(mailbox, message_id, destination))

    @mcp.tool(annotations=_write(idempotent=True))
    def mail_flag(
        mailbox: Annotated[str, Field(description="Mailbox name.")] = "INBOX",
        message_id: Annotated[str, Field(description="Numeric id from mail_search_messages.")] = ...,
        flagged: Annotated[bool, Field(description="True sets the flag, false clears it.")] = True,
    ) -> dict[str, Any]:
        """Set or clear the flagged star on one message."""
        return _run(mail, lambda: mail.flag(mailbox, message_id, flagged))

    @mcp.tool(name="mail_send_message", description=SEND, annotations=_write())
    def mail_send_message(
        to: Annotated[str, Field(description="Recipient addresses, separated by commas.")],
        subject: Annotated[str, Field(description="Subject line.")],
        body: Annotated[str, Field(description="Plain-text body.")],
        cc: Annotated[str | None, Field(description="Cc addresses, separated by commas.")] = None,
        bcc: Annotated[str | None, Field(description="Bcc addresses, separated by commas.")] = None,
    ) -> dict[str, Any]:
        return _run(mail, lambda: mail.send(to, subject, body, cc, bcc))

    @mcp.tool(name="mail_reply_to_message", description=REPLY, annotations=_write())
    def mail_reply_to_message(
        mailbox: Annotated[str, Field(description="Mailbox the original message is in.")],
        message_id: Annotated[str, Field(description="Numeric id of the message being answered.")],
        body: Annotated[str, Field(description="Plain-text reply. The original body is not quoted automatically.")],
        to: Annotated[str | None, Field(description="Override the reply address. Default is Reply-To or From.")] = None,
        cc: Annotated[str | None, Field(description="Cc addresses.")] = None,
        bcc: Annotated[str | None, Field(description="Bcc addresses.")] = None,
        subject: Annotated[str | None, Field(description="Override the subject. Default is Re: plus the original subject.")] = None,
    ) -> dict[str, Any]:
        return _run(mail, lambda: mail.reply(mailbox, message_id, body, to, cc, bcc, subject))

    @mcp.tool(annotations=_read())
    def calendar_list_calendars() -> dict[str, Any]:
        """List iCloud calendar names, including subscribed read-only calendars."""
        return _run(calendars, calendars.list_calendars)

    @mcp.tool(annotations=_read())
    def calendar_list_events(
        calendar: Annotated[str, Field(description="Calendar display name, for example Work Calendar.")],
        start: Annotated[str, Field(description=f"Inclusive start, {TZ_NAME}. YYYY-MM-DD or YYYY-MM-DDTHH:MM.")],
        end: Annotated[str, Field(description=f"Inclusive end. A date covers that whole {TZ_NAME} day.")],
        text: Annotated[str | None, Field(description="Optional case-insensitive match on summary, location, or description.")] = None,
        limit: Annotated[int, Field(description="Maximum events to return.", ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List or search events in one named calendar, for example Work Calendar, over a local-zone date range. Repeating events are expanded when iCloud does that."""
        return _run(calendars, lambda: calendars.list_events(calendar, start, end, text, limit))

    @mcp.tool(annotations=_read())
    def calendar_get_event(
        calendar: Annotated[str, Field(description="Calendar display name.")],
        uid: Annotated[str, Field(description="Event uid from calendar_list_events.")],
    ) -> dict[str, Any]:
        """Get one calendar event by uid."""
        return _run(calendars, lambda: calendars.get_event(calendar, uid))

    @mcp.tool(annotations=_write())
    def calendar_create_event(
        calendar: Annotated[str, Field(description="Calendar display name.")],
        summary: Annotated[str, Field(description="Event title.")],
        start: Annotated[str, Field(description=f"Start, {TZ_NAME}. YYYY-MM-DD or YYYY-MM-DDTHH:MM.")],
        end: Annotated[str | None, Field(description=f"End, {TZ_NAME}. Required unless all_day is true.")] = None,
        all_day: Annotated[bool, Field(description="True for an all-day event.")] = False,
        description: Annotated[str | None, Field(description="Notes.")] = None,
        location: Annotated[str | None, Field(description="Location.")] = None,
    ) -> dict[str, Any]:
        """Create one event on an iCloud calendar. Subscribed calendars are read-only and are refused. Timed values are stored in UTC; display uses the ICLOUD_TZ zone."""
        return _run(
            calendars,
            lambda: calendars.create_event(calendar, summary, start, end, all_day, description, location),
        )

    @mcp.tool(annotations=_write())
    def calendar_update_event(
        calendar: Annotated[str, Field(description="Calendar display name.")],
        uid: Annotated[str, Field(description="Event uid.")],
        summary: Annotated[str | None, Field(description="New title.")] = None,
        start: Annotated[str | None, Field(description="New start. Also pass end.")] = None,
        end: Annotated[str | None, Field(description="New end. Also pass start.")] = None,
        all_day: Annotated[bool | None, Field(description="Set true or false when changing the time.")] = None,
        description: Annotated[str | None, Field(description="New notes.")] = None,
        location: Annotated[str | None, Field(description="New location.")] = None,
    ) -> dict[str, Any]:
        """Update one calendar event. Changing the time requires both start and end. A repeating event is updated as a whole."""
        return _run(
            calendars,
            lambda: calendars.update_event(calendar, uid, summary, start, end, all_day, description, location),
        )

    @mcp.tool(name="calendar_delete_event", description=DELETE_EVENT, annotations=_write(destroy=True))
    def calendar_delete_event(
        calendar: Annotated[str, Field(description="Calendar display name.")],
        uid: Annotated[str, Field(description="Event uid to delete.")],
    ) -> dict[str, Any]:
        return _run(calendars, lambda: calendars.delete_event(calendar, uid))

    @mcp.tool(annotations=_read())
    def contacts_search(
        query: Annotated[str, Field(description="Text matched against name, email, phone, or organization.")],
        limit: Annotated[int, Field(description="Maximum contacts to return.", ge=1, le=50)] = 25,
    ) -> dict[str, Any]:
        """Search iCloud contacts."""
        return _run(contacts, lambda: contacts.search(query, limit))

    @mcp.tool(annotations=_read())
    def contacts_get(uid: Annotated[str, Field(description="Contact uid from contacts_search.")]) -> dict[str, Any]:
        """Get one contact, including the note."""
        return _run(contacts, lambda: contacts.get(uid))

    @mcp.tool(annotations=_write())
    def contacts_create(
        full_name: Annotated[str, Field(description="Name as it should be shown.")],
        email: Annotated[str | None, Field(description="Email address.")] = None,
        phone: Annotated[str | None, Field(description="Phone number.")] = None,
        organization: Annotated[str | None, Field(description="Organization.")] = None,
        note: Annotated[str | None, Field(description="Note.")] = None,
    ) -> dict[str, Any]:
        """Create one iCloud contact."""
        return _run(contacts, lambda: contacts.create(full_name, email, phone, organization, note))

    @mcp.tool(annotations=_write())
    def contacts_update(
        uid: Annotated[str, Field(description="Contact uid.")],
        full_name: Annotated[str | None, Field(description="New name.")] = None,
        email: Annotated[str | None, Field(description="Replaces the email addresses this connector writes. Empty clears them.")] = None,
        phone: Annotated[str | None, Field(description="Replaces the phone numbers this connector writes. Empty clears them.")] = None,
        organization: Annotated[str | None, Field(description="New organization.")] = None,
        note: Annotated[str | None, Field(description="New note.")] = None,
    ) -> dict[str, Any]:
        """Update one contact. Other vCard fields, including a photo, are kept."""
        return _run(contacts, lambda: contacts.update(uid, full_name, email, phone, organization, note))

    @mcp.tool(annotations=_read())
    def reminders_list_lists() -> dict[str, Any]:
        """List reminder lists that iCloud still exposes over CalDAV. Upgraded lists may be missing."""
        return _run(reminders, reminders.list_lists)

    @mcp.tool(annotations=_read())
    def reminders_list(
        list_name: Annotated[
            str,
            Field(description="Reminder list name from reminders_list_lists.", validation_alias="list"),
        ],
        include_completed: Annotated[bool, Field(description="When true, include completed reminders.")] = False,
        limit: Annotated[int, Field(description="Maximum reminders to return.", ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List reminders in one list. If the list was upgraded, the result says so and does not invent items."""
        return _run(reminders, lambda: reminders.list_reminders(list_name, include_completed, limit))

    @mcp.tool(annotations=_write())
    def reminders_add(
        list_name: Annotated[str, Field(description="Reminder list name.", validation_alias="list")],
        title: Annotated[str, Field(description="Reminder title.")],
        due: Annotated[str | None, Field(description=f"Due date or time, {TZ_NAME}. YYYY-MM-DD or YYYY-MM-DDTHH:MM.")] = None,
        notes: Annotated[str | None, Field(description="Notes.")] = None,
        priority: Annotated[str | None, Field(description="high, medium, or low.")] = None,
    ) -> dict[str, Any]:
        """Add one reminder to a list that is still available over CalDAV."""
        return _run(reminders, lambda: reminders.add(list_name, title, due, notes, priority))

    @mcp.tool(annotations=_write(idempotent=True))
    def reminders_complete(
        list_name: Annotated[str, Field(description="Reminder list name.", validation_alias="list")],
        uid: Annotated[str, Field(description="Reminder uid.")],
    ) -> dict[str, Any]:
        """Mark one reminder complete. This checks it off. It does not delete the reminder."""
        return _run(reminders, lambda: reminders.complete(list_name, uid))

    @mcp.tool(annotations=_write())
    def reminders_edit(
        list_name: Annotated[str, Field(description="Reminder list name.", validation_alias="list")],
        uid: Annotated[str, Field(description="Reminder uid.")],
        title: Annotated[str | None, Field(description="New title.")] = None,
        due: Annotated[str | None, Field(description=f"New due date or time, {TZ_NAME}.")] = None,
        notes: Annotated[str | None, Field(description="New notes.")] = None,
        priority: Annotated[str | None, Field(description="high, medium, or low.")] = None,
    ) -> dict[str, Any]:
        """Edit one reminder that is still available over CalDAV."""
        return _run(reminders, lambda: reminders.edit(list_name, uid, title, due, notes, priority))


def build_server(mail: MailClient, dav: DavClient) -> MCPServer:
    mcp = MCPServer(
        "icloud",
        instructions=INSTRUCTIONS,
        version="1.0.0",
    )
    register(mcp, mail, dav)
    return mcp
