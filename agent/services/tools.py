"""The agent's tools.

Read tools run straight away. Write tools never change anything when the model calls them: they are
validated, then stored as an Action waiting for a person. The change happens only in `execute()`,
which runs when someone presses Approve, and validates again in case the data moved in between.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import Action, Customer, Email, Event, Order, ToolCall, Workspace

OPEN_HOUR, CLOSE_HOUR = 9, 18  # shop hours for appointments


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


READ_TOOLS = [
    {
        "name": "search_customers",
        "description": "Find customers by name, email or customer ID. Returns up to 5 matches with their contact details.",
        "input_schema": _schema({"query": {"type": "string", "description": "Name, email or customer ID, e.g. 'Anna' or 'C-101'"}}),
    },
    {
        "name": "get_order",
        "description": "Get one order by its order ID, including total, amount already refunded and status.",
        "input_schema": _schema({"order_id": {"type": "string", "description": "Order ID, e.g. 'AT-1042'"}}),
    },
    {
        "name": "list_orders",
        "description": "List orders, optionally for one customer and/or with one status.",
        "input_schema": _schema({
            "customer_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Customer ID, or null for all customers"},
            "status": {"type": "string", "enum": ["any", "paid", "shipped", "delivered", "refunded"]},
        }),
    },
    {
        "name": "check_calendar",
        "description": f"List the shop's appointments on one date. Appointments can be booked between {OPEN_HOUR}:00 and {CLOSE_HOUR}:00.",
        "input_schema": _schema({"date": {"type": "string", "format": "date", "description": "YYYY-MM-DD"}}),
    },
]

WRITE_TOOLS = [
    {
        "name": "send_email",
        "description": "Propose an email to a customer. A person must approve it before it is sent.",
        "input_schema": _schema({
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain text, signed 'Alpine Trail Bikes'"},
        }),
    },
    {
        "name": "create_event",
        "description": "Propose a calendar appointment. A person must approve it before it is booked. Check the calendar first.",
        "input_schema": _schema({
            "title": {"type": "string"},
            "start": {"type": "string", "description": "Local start time as YYYY-MM-DDTHH:MM"},
            "duration_minutes": {"type": "integer", "description": "Between 15 and 240"},
            "attendee_email": {"type": "string", "description": "Customer email, or an empty string"},
        }),
    },
    {
        "name": "issue_refund",
        "description": "Propose a refund on an order. A person must approve it before money is returned. The amount cannot exceed what is still refundable.",
        "input_schema": _schema({
            "order_id": {"type": "string"},
            "amount_chf": {"type": "number"},
            "reason": {"type": "string"},
        }),
    },
]

TOOLS = [{**tool, "strict": True} for tool in READ_TOOLS + WRITE_TOOLS]
WRITE_NAMES = {tool["name"] for tool in WRITE_TOOLS}


class ToolError(Exception):
    """A problem the model can fix, returned to it as an error tool result."""


# ---- read tools --------------------------------------------------------------------------
def search_customers(ws: Workspace, query: str) -> list[dict]:
    query = query.strip()
    if not query:
        raise ToolError("Give a name, email or customer ID to search for.")
    matches = ws.customers.filter(Q(name__icontains=query) | Q(email__icontains=query) | Q(code__iexact=query))
    if not matches.exists():
        # "Anna K." or "Keller, Anna": fall back to matching any single word.
        words = [w for w in query.replace(",", " ").replace(".", " ").split() if len(w) > 1]
        condition = Q()
        for word in words:
            condition |= Q(name__icontains=word)
        matches = ws.customers.filter(condition) if words else matches
    return [c.as_dict() for c in matches[:5]]


def get_order(ws: Workspace, order_id: str) -> dict:
    order = ws.orders.select_related("customer").filter(code__iexact=order_id.strip()).first()
    if order is None:
        raise ToolError(f"No order with ID {order_id}.")
    return order.as_dict()


def list_orders(ws: Workspace, customer_id: str | None, status: str) -> list[dict]:
    orders = ws.orders.select_related("customer")
    if customer_id:
        orders = orders.filter(customer__code__iexact=customer_id.strip())
    if status != "any":
        orders = orders.filter(status=status)
    return [o.as_dict() for o in orders[:20]]


def check_calendar(ws: Workspace, day: str) -> dict:
    try:
        when = date.fromisoformat(day)
    except ValueError as exc:
        raise ToolError("Use the date format YYYY-MM-DD.") from exc
    events = [e.as_dict() for e in ws.events.filter(start__date=when)]
    return {"date": when.isoformat(), "weekday": when.strftime("%A"), "opening_hours": f"{OPEN_HOUR}:00-{CLOSE_HOUR}:00", "appointments": events}


READERS = {
    "search_customers": lambda ws, p: search_customers(ws, p["query"]),
    "get_order": lambda ws, p: get_order(ws, p["order_id"]),
    "list_orders": lambda ws, p: list_orders(ws, p.get("customer_id"), p.get("status", "any")),
    "check_calendar": lambda ws, p: check_calendar(ws, p["date"]),
}


# ---- write tools: validation (at proposal and again at approval) --------------------------
def _parse_start(value: str) -> datetime:
    try:
        naive = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ToolError("Use the start format YYYY-MM-DDTHH:MM.") from exc
    return timezone.make_aware(naive)


def validate_write(ws: Workspace, name: str, p: dict) -> tuple[dict, str]:
    """Check a proposed change. Returns cleaned parameters and a one-line summary for the approver."""
    if name == "send_email":
        to, subject, body = p["to"].strip(), p["subject"].strip(), p["body"].strip()
        try:
            validate_email(to)
        except ValidationError as exc:
            raise ToolError(f"'{to}' is not a valid email address.") from exc
        if not ws.customers.filter(email__iexact=to).exists():
            raise ToolError("Emails can only go to existing customers. Look the address up with search_customers.")
        if not subject or not body:
            raise ToolError("An email needs a subject and a body.")
        return {"to": to, "subject": subject[:200], "body": body[:4000]}, f"Email {to}: {subject[:80]}"

    if name == "create_event":
        start = _parse_start(p["start"])
        minutes = int(p["duration_minutes"])
        if not 15 <= minutes <= 240:
            raise ToolError("Appointments last between 15 and 240 minutes.")
        end = start + timedelta(minutes=minutes)
        local_start, local_end = timezone.localtime(start), timezone.localtime(end)
        if start < timezone.now():
            raise ToolError("That time is in the past.")
        if local_start.weekday() == 6:
            raise ToolError("The shop is closed on Sundays.")
        if local_start.hour < OPEN_HOUR or (local_end.hour, local_end.minute) > (CLOSE_HOUR, 0) or local_end.date() != local_start.date():
            raise ToolError(f"Appointments must be between {OPEN_HOUR}:00 and {CLOSE_HOUR}:00.")
        for event in ws.events.filter(start__date=local_start.date()):
            if start < event.start + timedelta(minutes=event.minutes) and event.start < end:
                raise ToolError(f"That clashes with '{event.title}' at {timezone.localtime(event.start):%H:%M}. Check the calendar and pick a free slot.")
        attendee = p["attendee_email"].strip()
        if attendee:
            try:
                validate_email(attendee)
            except ValidationError as exc:
                raise ToolError(f"'{attendee}' is not a valid email address.") from exc
        title = p["title"].strip()[:160] or "Appointment"
        cleaned = {"title": title, "start": local_start.strftime("%Y-%m-%dT%H:%M"), "duration_minutes": minutes, "attendee_email": attendee}
        return cleaned, f"Book '{title}' on {local_start:%a %d %b %Y, %H:%M} for {minutes} min"

    if name == "issue_refund":
        order = ws.orders.filter(code__iexact=p["order_id"].strip()).first()
        if order is None:
            raise ToolError(f"No order with ID {p['order_id']}.")
        try:
            amount = Decimal(str(p["amount_chf"])).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise ToolError("The refund amount must be a number.") from exc
        refundable = order.total - order.refunded
        if amount <= 0:
            raise ToolError("The refund amount must be more than zero.")
        if amount > refundable:
            raise ToolError(f"Only CHF {refundable} is still refundable on {order.code}.")
        reason = p["reason"].strip()[:200]
        if not reason:
            raise ToolError("Give a reason for the refund.")
        return {"order_id": order.code, "amount_chf": float(amount), "reason": reason}, f"Refund CHF {amount} on {order.code} ({reason[:60]})"

    raise ToolError(f"Unknown tool {name}.")


@transaction.atomic
def execute(action: Action) -> str:
    """Carry out an approved action. Returns a short result for the log and the chat."""
    ws, p = action.workspace, action.params
    cleaned, _ = validate_write(ws, action.tool, p)  # the data may have changed since the proposal
    if action.tool == "send_email":
        Email.objects.create(workspace=ws, to=cleaned["to"], subject=cleaned["subject"], body=cleaned["body"])
        return f"Email to {cleaned['to']} placed in the outbox."
    if action.tool == "create_event":
        start = _parse_start(cleaned["start"])
        Event.objects.create(workspace=ws, title=cleaned["title"], start=start, minutes=cleaned["duration_minutes"], attendee=cleaned["attendee_email"])
        return f"'{cleaned['title']}' booked for {timezone.localtime(start):%a %d %b, %H:%M}."
    if action.tool == "issue_refund":
        order = Order.objects.select_for_update().get(workspace=ws, code=cleaned["order_id"])
        order.refunded += Decimal(str(cleaned["amount_chf"]))
        if order.refunded >= order.total:
            order.status = "refunded"
        order.save()
        return f"CHF {Decimal(str(cleaned['amount_chf'])):.2f} refunded on {order.code}."
    raise ToolError(f"Unknown tool {action.tool}.")


# ---- dispatch ----------------------------------------------------------------------------
def run(ws: Workspace, name: str, params: dict) -> tuple[str, bool]:
    """Run one tool call from the model. Returns (text for the tool_result, is_error)."""
    params = params if isinstance(params, dict) else {}
    try:
        if name in READERS:
            result, is_error = json.dumps(READERS[name](ws, params), ensure_ascii=False), False
        elif name in WRITE_NAMES:
            cleaned, summary = validate_write(ws, name, params)
            action = Action.objects.create(workspace=ws, tool=name, params=cleaned, summary=summary)
            result = (
                f"Not done yet. Queued as action #{action.pk} for a person to approve: {summary}. "
                "Tell the user it is waiting for their approval. Do not say it has been done."
            )
            is_error = False
        else:
            raise ToolError(f"Unknown tool {name}.")
    except ToolError as exc:
        result, is_error = f"Error: {exc}", True
    except (KeyError, TypeError, ValueError) as exc:
        result, is_error = f"Error: missing or invalid input ({exc}).", True
    ToolCall.objects.create(workspace=ws, tool=name, params=params, result=result[:4000], is_write=name in WRITE_NAMES, is_error=is_error)
    return result, is_error


def customers_exist(ws: Workspace) -> bool:
    return Customer.objects.filter(workspace=ws).exists()
