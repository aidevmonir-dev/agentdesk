"""Seed a visitor's workspace with a fictional bike shop. Dates are relative to today, so the demo never ages."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from ..models import Customer, Event, Order, Workspace

CUSTOMERS = [
    ("C-101", "Anna Keller", "anna.keller@example.com", "Zürich", "Regular since 2023. Prefers email."),
    ("C-102", "Jonas Meier", "jonas.meier@example.com", "Bern", "Interested in a bike fitting."),
    ("C-103", "Sofia Rossi", "sofia.rossi@example.com", "Lugano", ""),
    ("C-104", "Lukas Brunner", "lukas.brunner@example.com", "Luzern", "Business account for a hotel's rental fleet."),
    ("C-105", "Emma Schmid", "emma.schmid@example.com", "Basel", ""),
]

# (order, customer, items, total, status, days ago)
ORDERS = [
    ("AT-1042", "C-101", "Trail helmet M, cycling gloves L", "189.00", "delivered", 6),
    ("AT-1051", "C-102", "Gravel bike Alpina G2, size L", "2490.00", "shipped", 3),
    ("AT-1055", "C-103", "E-bike battery 625 Wh", "749.00", "paid", 2),
    ("AT-1057", "C-104", "8 x rental helmets, 8 x bike locks", "936.00", "paid", 1),
    ("AT-1060", "C-105", "Tubeless repair kit", "34.90", "delivered", 12),
    ("AT-1061", "C-101", "Bike lights set", "79.00", "paid", 0),
]


def next_weekday(start: date, weekday: int) -> date:
    """The next given weekday strictly after `start` (Monday = 0)."""
    days = (weekday - start.weekday()) % 7 or 7
    return start + timedelta(days=days)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


def seed(workspace: Workspace) -> None:
    today = timezone.localdate()
    customers = {
        code: Customer.objects.create(workspace=workspace, code=code, name=name, email=email, city=city, notes=notes)
        for code, name, email, city, notes in CUSTOMERS
    }
    for code, customer, items, total, status, days_ago in ORDERS:
        Order.objects.create(
            workspace=workspace, code=code, customer=customers[customer], items=items,
            total=Decimal(total), status=status, ordered_on=today - timedelta(days=days_ago),
        )
    tuesday = next_weekday(today, 1)
    for day, hour, minute, minutes, title in [
        (tuesday, 9, 0, 60, "Team stand-up and workshop planning"),
        (tuesday, 14, 0, 60, "Supplier call: Alpina bikes"),
        (tuesday, 16, 30, 30, "Test ride: walk-in customer"),
        (next_weekday(today, 2), 10, 0, 90, "Inventory count"),
    ]:
        Event.objects.create(workspace=workspace, title=title, start=_at(day, hour, minute), minutes=minutes)
