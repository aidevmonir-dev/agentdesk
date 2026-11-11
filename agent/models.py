"""Each visitor gets a private copy of a fictional shop's data, so the agent's actions never collide."""
import uuid

from django.db import models


class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return str(self.id)


class Customer(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="customers")
    code = models.CharField(max_length=12)
    name = models.CharField(max_length=120)
    email = models.EmailField()
    city = models.CharField(max_length=80)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["code"]

    def as_dict(self) -> dict:
        return {"customer_id": self.code, "name": self.name, "email": self.email, "city": self.city, "notes": self.notes}


class Order(models.Model):
    STATUSES = ["paid", "shipped", "delivered", "refunded"]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="orders")
    code = models.CharField(max_length=12)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="orders")
    items = models.CharField(max_length=300)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    refunded = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=12)
    ordered_on = models.DateField()

    class Meta:
        ordering = ["-ordered_on", "code"]

    def as_dict(self) -> dict:
        return {
            "order_id": self.code,
            "customer_id": self.customer.code,
            "customer_name": self.customer.name,
            "items": self.items,
            "total_chf": float(self.total),
            "refunded_chf": float(self.refunded),
            "refundable_chf": float(self.total - self.refunded),
            "status": self.status,
            "ordered_on": self.ordered_on.isoformat(),
        }


class Event(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="events")
    title = models.CharField(max_length=160)
    start = models.DateTimeField()
    minutes = models.PositiveIntegerField()
    attendee = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ["start"]

    def as_dict(self) -> dict:
        return {"title": self.title, "start": self.start.strftime("%Y-%m-%d %H:%M"), "minutes": self.minutes, "attendee": self.attendee}


class Email(models.Model):
    """The demo outbox. Approved emails land here; nothing is ever sent from the server."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="emails")
    to = models.CharField(max_length=160)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Action(models.Model):
    """A change the agent proposed. It runs only after a person approves it."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for approval"
        DONE = "done", "Approved and done"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Could not be done"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="actions")
    tool = models.CharField(max_length=40)
    params = models.JSONField()
    summary = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    result = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class ToolCall(models.Model):
    """The activity log: every tool the agent used, with what it sent and what came back."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tool_calls")
    tool = models.CharField(max_length=40)
    params = models.JSONField()
    result = models.TextField()
    is_write = models.BooleanField(default=False)
    is_error = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "You"
        ASSISTANT = "assistant", "Agent"
        NOTE = "note", "Update"  # approvals and rejections, shown in the chat and passed to the agent

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=Role.choices)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
