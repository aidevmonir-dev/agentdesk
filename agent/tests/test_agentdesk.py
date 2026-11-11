import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from agent.models import Action, Email, Event, Message, Order, ToolCall, Workspace
from agent.services import tools
from agent.services.demo_data import next_weekday, seed
from agent.services.runner import run_script, run_turn

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool_use(name, tool_input, block_id):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def response(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class FakeClient:
    """Plays back scripted model responses, so tests never call the network."""

    def __init__(self, *responses):
        self.calls = []
        self._responses = list(responses)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


def future_slot(hour=15):
    day = next_weekday(timezone.localdate(), 1)  # next Tuesday, always in the future
    return f"{day.isoformat()}T{hour:02d}:00"


class ToolTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create()
        seed(self.ws)

    def test_read_tools(self):
        found = json.loads(tools.run(self.ws, "search_customers", {"query": "Keller, Anna"})[0])
        self.assertEqual(found[0]["customer_id"], "C-101")
        order = json.loads(tools.run(self.ws, "get_order", {"order_id": "at-1042"})[0])
        self.assertEqual(order["refundable_chf"], 189.0)
        paid = json.loads(tools.run(self.ws, "list_orders", {"customer_id": None, "status": "paid"})[0])
        self.assertEqual({o["order_id"] for o in paid}, {"AT-1055", "AT-1057", "AT-1061"})
        calendar = json.loads(tools.run(self.ws, "check_calendar", {"date": future_slot()[:10]})[0])
        self.assertEqual(len(calendar["appointments"]), 3)

    def test_write_tools_only_queue(self):
        result, is_error = tools.run(self.ws, "issue_refund", {"order_id": "AT-1042", "amount_chf": 189, "reason": "Damaged"})
        self.assertFalse(is_error)
        self.assertIn("Not done yet", result)
        self.assertEqual(Order.objects.get(code="AT-1042").refunded, 0)  # nothing changed
        action = Action.objects.get()
        self.assertEqual(action.status, Action.Status.PENDING)
        self.assertEqual(action.params["amount_chf"], 189.0)

    def test_guard_rails(self):
        cases = [
            ("issue_refund", {"order_id": "AT-1060", "amount_chf": 50, "reason": "Goodwill"}, "still refundable"),
            ("issue_refund", {"order_id": "AT-9999", "amount_chf": 5, "reason": "x"}, "No order"),
            ("send_email", {"to": "stranger@example.org", "subject": "Hi", "body": "Hello"}, "existing customers"),
            ("create_event", {"title": "Fitting", "start": future_slot(14), "duration_minutes": 30, "attendee_email": ""}, "clashes"),
            ("create_event", {"title": "Fitting", "start": future_slot(19), "duration_minutes": 30, "attendee_email": ""}, "between 9:00"),
            ("create_event", {"title": "Fitting", "start": "2020-01-07T10:00", "duration_minutes": 30, "attendee_email": ""}, "past"),
            ("unknown_tool", {}, "Unknown tool"),
        ]
        for name, params, expected in cases:
            result, is_error = tools.run(self.ws, name, params)
            self.assertTrue(is_error, name)
            self.assertIn(expected, result, name)
        self.assertEqual(Action.objects.count(), 0)
        self.assertEqual(ToolCall.objects.filter(is_error=True).count(), len(cases))

    def test_execute_revalidates(self):
        tools.run(self.ws, "issue_refund", {"order_id": "AT-1042", "amount_chf": 100, "reason": "Partial"})
        tools.run(self.ws, "issue_refund", {"order_id": "AT-1042", "amount_chf": 100, "reason": "Partial again"})
        first, second = Action.objects.order_by("id")
        self.assertIn("refunded", tools.execute(first))
        with self.assertRaises(tools.ToolError):  # only 89.00 left by now
            tools.execute(second)

    def test_full_refund_marks_order_refunded(self):
        tools.run(self.ws, "issue_refund", {"order_id": "AT-1042", "amount_chf": 189, "reason": "Damaged"})
        tools.execute(Action.objects.get())
        order = Order.objects.get(code="AT-1042")
        self.assertEqual(order.refunded, Decimal("189.00"))
        self.assertEqual(order.status, "refunded")

    def test_tool_definitions_are_strict(self):
        for tool in tools.TOOLS:
            schema = tool["input_schema"]
            self.assertTrue(tool["strict"])
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(set(schema["required"]), set(schema["properties"]))


class RunnerTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create()
        seed(self.ws)

    def test_agent_loop_proposes_and_reports(self):
        first = response([
            tool_use("search_customers", {"query": "Anna Keller"}, "t1"),
            tool_use("get_order", {"order_id": "AT-1042"}, "t2"),
        ], "tool_use")
        second = response([tool_use("issue_refund", {"order_id": "AT-1042", "amount_chf": 189, "reason": "Damaged"}, "t3")], "tool_use")
        final = response([text("I've queued a CHF 189 refund for your approval.")])
        client = FakeClient(first, second, final)

        reply = run_turn(self.ws, "Refund Anna's damaged order", client=client)
        self.assertEqual(reply.text, "I've queued a CHF 189 refund for your approval.")
        self.assertEqual(Action.objects.filter(status="pending").count(), 1)

        # Both tool results of the first round go back in one user message, in order.
        results = client.calls[1]["messages"][-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2"])
        self.assertEqual(client.calls[1]["messages"][-2], {"role": "assistant", "content": first.content})
        call = client.calls[0]
        self.assertEqual(len(call["tools"]), 7)
        self.assertNotIn("tool_choice", call)
        self.assertIn("approves or rejects", call["system"])

    def test_tool_errors_go_back_to_the_model(self):
        client = FakeClient(
            response([tool_use("issue_refund", {"order_id": "AT-1060", "amount_chf": 50, "reason": "x"}, "t1")], "tool_use"),
            response([text("Only CHF 34.90 can be refunded on that order.")]),
        )
        run_turn(self.ws, "Refund 50 on AT-1060", client=client)
        result = client.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertEqual(Action.objects.count(), 0)

    def test_loop_is_bounded(self):
        looping = [response([tool_use("list_orders", {"customer_id": None, "status": "any"}, f"t{i}")], "tool_use") for i in range(20)]
        client = FakeClient(*looping)
        with self.settings(AGENTDESK={**__import__("django.conf").conf.settings.AGENTDESK, "MAX_TOOL_ROUNDS": 3}):
            reply = run_turn(self.ws, "loop", client=client)
        self.assertEqual(len(client.calls), 3)
        self.assertIn("too many steps", reply.text)

    def test_history_includes_approval_updates(self):
        Message.objects.create(workspace=self.ws, role="user", text="Refund it")
        Message.objects.create(workspace=self.ws, role="assistant", text="Queued.")
        Message.objects.create(workspace=self.ws, role="note", text="Approved and done: action #1.")
        client = FakeClient(response([text("Great, it's done.")]))
        run_turn(self.ws, "Is it done?", client=client)
        messages = client.calls[0]["messages"]
        self.assertEqual(messages[2]["role"], "user")
        self.assertTrue(messages[2]["content"].startswith("[Update from the approval queue]"))
        self.assertEqual(messages[-1], {"role": "user", "content": "Is it done?"})

    def test_refusal(self):
        reply = run_turn(self.ws, "x", client=FakeClient(response([], "refusal")))
        self.assertIn("can't help", reply.text)

    def test_scripts_use_the_real_tools(self):
        run_script(self.ws, "damaged")
        self.assertEqual(sorted(Action.objects.values_list("tool", flat=True)), ["issue_refund", "send_email"])
        run_script(self.ws, "fitting")
        self.assertEqual(Action.objects.filter(tool="create_event").count(), 1)
        run_script(self.ws, "unshipped")
        reply = run_script(self.ws, "guardrail")
        self.assertIn("refused", reply.text)
        self.assertEqual(Action.objects.count(), 4)  # the guard-rail script queued nothing


@override_settings(CACHES=LOCMEM, ANTHROPIC_API_KEY="")
class ApiTests(TestCase):
    def setUp(self):
        cache.clear()

    def chat(self, **payload):
        return self.client.post("/api/chat", json.dumps(payload), content_type="application/json")

    def test_page_and_state(self):
        self.assertContains(self.client.get("/"), "asks before it acts")
        state = self.client.get("/api/state").json()
        self.assertEqual(len(state["data"]["orders"]), 6)
        self.assertEqual(state["actions"], [])

    def test_approve_and_reject_flow(self):
        state = self.chat(script="damaged").json()["state"]
        pending = {a["tool"]: a["id"] for a in state["actions"]}
        approved = self.client.post(f"/api/actions/{pending['issue_refund']}/approve").json()["state"]
        rejected = self.client.post(f"/api/actions/{pending['send_email']}/reject").json()["state"]
        statuses = {a["tool"]: a["status"] for a in rejected["actions"]}
        self.assertEqual(statuses, {"issue_refund": "done", "send_email": "rejected"})
        self.assertEqual(Order.objects.get(code="AT-1042").status, "refunded")
        self.assertEqual(Email.objects.count(), 0)
        self.assertEqual([m["role"] for m in rejected["messages"]][-2:], ["note", "note"])
        self.assertTrue(approved["actions"])
        again = self.client.post(f"/api/actions/{pending['issue_refund']}/approve")
        self.assertEqual(again.status_code, 409)

    def test_approved_booking_and_email(self):
        state = self.chat(script="fitting").json()["state"]
        for action in state["actions"]:
            self.client.post(f"/api/actions/{action['id']}/approve")
        self.assertTrue(Event.objects.filter(title__startswith="Bike fitting").exists())
        self.assertEqual(Email.objects.get().to, "jonas.meier@example.com")

    def test_free_text_without_model(self):
        reply = self.chat(text="Email Sofia").json()
        self.assertEqual(reply["mode"], "scripted")
        self.assertIn("No AI model", reply["reply"])

    def test_workspaces_are_private(self):
        state = self.chat(script="damaged").json()["state"]
        other = self.client_class()
        self.assertEqual(other.get("/api/state").json()["actions"], [])
        self.assertEqual(other.post(f"/api/actions/{state['actions'][0]['id']}/approve").status_code, 404)

    def test_bad_requests_and_limits(self):
        from django.conf import settings

        self.assertEqual(self.chat(text="").status_code, 400)
        self.assertEqual(self.chat(script="nope").status_code, 400)
        self.assertEqual(self.client.post("/api/actions/1/explode").status_code, 404)
        with self.settings(AGENTDESK={**settings.AGENTDESK, "MESSAGES_PER_IP_PER_DAY": 1}):
            self.assertEqual(self.chat(script="unshipped").status_code, 200)
            self.assertEqual(self.chat(script="unshipped").status_code, 429)

    def test_reset_and_expiry(self):
        self.chat(script="damaged")
        state = self.client.post("/api/reset").json()["state"]
        self.assertEqual(state["actions"], [])
        self.assertEqual(Workspace.objects.count(), 1)
        Workspace.objects.update(created_at=timezone.now() - timedelta(days=2))
        from agent.views import purge_expired

        purge_expired()
        self.assertEqual(Workspace.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)
