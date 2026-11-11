from __future__ import annotations

import json
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import limits
from .models import Action, Message, Workspace
from .services import tools
from .services.demo_data import seed
from .services.runner import SCRIPTS, run_script, run_turn

MAX_MESSAGE_CHARS = 1000


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def purge_expired() -> int:
    cutoff = timezone.now() - timedelta(hours=settings.AGENTDESK["RETENTION_HOURS"])
    return Workspace.objects.filter(created_at__lt=cutoff).delete()[0]


def _workspace(request: HttpRequest) -> Workspace:
    """Every visitor works on a private copy of the demo shop."""
    workspace = Workspace.objects.filter(pk=request.session.get("workspace")).first() if request.session.get("workspace") else None
    if workspace is None:
        purge_expired()
        workspace = Workspace.objects.create()
        seed(workspace)
        request.session["workspace"] = str(workspace.pk)
    return workspace


def _ordered_actions(ws: Workspace) -> list[Action]:
    """Waiting requests first, in the order they were proposed; decided ones after, newest first."""
    pending = list(ws.actions.filter(status=Action.Status.PENDING).order_by("created_at", "id"))
    decided = list(ws.actions.exclude(status=Action.Status.PENDING).order_by("-decided_at", "-id"))
    return pending + decided


def _state(ws: Workspace) -> dict:
    local = timezone.localtime
    return {
        "messages": [{"role": m.role, "text": m.text} for m in ws.messages.all()],
        "actions": [
            {"id": a.pk, "tool": a.tool, "summary": a.summary, "params": a.params, "status": a.status,
             "status_label": a.get_status_display(), "result": a.result}
            for a in _ordered_actions(ws)
        ],
        "activity": [
            {"tool": c.tool, "params": c.params, "result": c.result[:600], "is_write": c.is_write, "is_error": c.is_error, "at": local(c.created_at).strftime("%H:%M:%S")}
            for c in ws.tool_calls.all()[:40]
        ],
        "data": {
            "orders": [o.as_dict() for o in ws.orders.select_related("customer")],
            "events": [e.as_dict() | {"start": local(e.start).strftime("%a %d %b %H:%M")} for e in ws.events.filter(start__gte=timezone.now() - timedelta(days=1))],
            "outbox": [{"to": e.to, "subject": e.subject, "body": e.body} for e in ws.emails.all()],
            "customers": [c.as_dict() for c in ws.customers.all()],
        },
    }


@ensure_csrf_cookie
@require_GET
def index(request: HttpRequest):
    _workspace(request)
    return render(request, "agent/index.html", {
        "scripts": {key: prompt for key, (prompt, _) in SCRIPTS.items()},
        "ai_enabled": bool(settings.ANTHROPIC_API_KEY),
        "repo_url": settings.AGENTDESK["REPO_URL"],
        "retention_hours": settings.AGENTDESK["RETENTION_HOURS"],
    })


@require_GET
def state(request: HttpRequest):
    return JsonResponse(_state(_workspace(request)))


def _allowed(request: HttpRequest, ws: Workspace) -> str | None:
    cfg = settings.AGENTDESK
    if ws.messages.filter(role=Message.Role.USER).count() >= cfg["MAX_MESSAGES_PER_WORKSPACE"]:
        return "This demo session is full. Reset the demo to start again."
    if not limits.allow("msg", limits.client_ip(request), cfg["MESSAGES_PER_IP_PER_DAY"]):
        return "You have reached today's limit for this demo. Try again tomorrow."
    if not limits.allow("msg-total", "everyone", cfg["MESSAGES_PER_DAY_TOTAL"]):
        return "This demo has reached its daily limit. Try again tomorrow."
    return None


@require_POST
def chat(request: HttpRequest):
    ws = _workspace(request)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _error("Send the message as JSON.")
    script = payload.get("script")
    text = str(payload.get("text", "")).strip()
    if script and script not in SCRIPTS:
        return _error("Unknown example task.")
    if not script and not text:
        return _error("Type a request first.")
    if len(text) > MAX_MESSAGE_CHARS:
        return _error(f"Keep requests under {MAX_MESSAGE_CHARS} characters.")
    blocked = _allowed(request, ws)
    if blocked:
        return _error(blocked, 429)
    # With a model connected, examples go through the model like any other request.
    result = run_script(ws, script) if script and not settings.ANTHROPIC_API_KEY else run_turn(ws, SCRIPTS[script][0] if script else text)
    return JsonResponse({"reply": result.text, "mode": result.mode, "state": _state(ws)})


@require_POST
def decide(request: HttpRequest, pk: int, decision: str):
    if decision not in {"approve", "reject"}:
        return _error("Use approve or reject.", 404)
    ws = _workspace(request)
    with transaction.atomic():
        action = Action.objects.select_for_update().filter(workspace=ws, pk=pk).first()
        if action is None:
            return _error("That action was not found.", 404)
        if action.status != Action.Status.PENDING:
            return _error("That action has already been decided.", 409)
        action.decided_at = timezone.now()
        if decision == "approve":
            try:
                action.result = tools.execute(action)
                action.status = Action.Status.DONE
            except tools.ToolError as exc:
                action.result = str(exc)
                action.status = Action.Status.FAILED
        else:
            action.status = Action.Status.REJECTED
            action.result = "Rejected by staff."
        action.save()
        verb = {"done": "Approved and done", "failed": "Approved but it could not be done", "rejected": "Rejected"}[action.status]
        Message.objects.create(workspace=ws, role=Message.Role.NOTE, text=f"{verb}: action #{action.pk}, {action.summary}. {action.result}")
    return JsonResponse({"state": _state(ws)})


@require_POST
def reset(request: HttpRequest):
    ws = Workspace.objects.filter(pk=request.session.get("workspace")).first() if request.session.get("workspace") else None
    if ws:
        ws.delete()
    request.session.pop("workspace", None)
    return JsonResponse({"state": _state(_workspace(request))})


@require_GET
def healthz(request: HttpRequest):
    return JsonResponse({"ok": True, "ai": bool(settings.ANTHROPIC_API_KEY)})
