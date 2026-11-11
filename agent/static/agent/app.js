(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var thread = $("thread"), form = $("ask"), input = $("text"), send = $("send");
  var busy = false;

  var TOOL_LABELS = {
    search_customers: "Searched customers", get_order: "Looked up an order", list_orders: "Listed orders",
    check_calendar: "Checked the calendar", send_email: "Proposed an email", create_event: "Proposed a booking",
    issue_refund: "Proposed a refund",
  };
  var ACTION_TITLES = { send_email: "Send an email", create_event: "Book an appointment", issue_refund: "Issue a refund" };

  function csrf() {
    var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function api(url, payload) {
    var options = { credentials: "same-origin", headers: { "X-CSRFToken": csrf() } };
    if (payload !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    return fetch(url, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error(data.error || "Something went wrong. Try again.");
        return data;
      });
    });
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // ---- chat --------------------------------------------------------------------------------
  function renderMessages(messages) {
    var intro = $("intro");
    thread.textContent = "";
    thread.appendChild(intro);
    intro.hidden = messages.length > 0;
    messages.forEach(function (m) {
      var row = el("div", "msg msg-" + m.role);
      if (m.role === "note") row.appendChild(el("span", "note-label", "Update"));
      row.appendChild(el("p", "", m.text));
      thread.appendChild(row);
    });
    thread.scrollTop = thread.scrollHeight;
  }

  // ---- side panels -------------------------------------------------------------------------
  function paramList(action) {
    var dl = el("dl", "params");
    var p = action.params;
    var rows = action.tool === "send_email" ? [["To", p.to], ["Subject", p.subject], ["Message", p.body]]
      : action.tool === "create_event" ? [["What", p.title], ["When", p.start.replace("T", " ")], ["Length", p.duration_minutes + " min"], ["With", p.attendee_email || "-"]]
      : [["Order", p.order_id], ["Amount", "CHF " + Number(p.amount_chf).toFixed(2)], ["Reason", p.reason]];
    rows.forEach(function (r) {
      var div = el("div");
      div.appendChild(el("dt", "", r[0]));
      div.appendChild(el("dd", r[0] === "Message" ? "body" : "", r[1]));
      dl.appendChild(div);
    });
    return dl;
  }

  function renderQueue(actions) {
    var panel = $("panel-queue");
    panel.textContent = "";
    var pending = actions.filter(function (a) { return a.status === "pending"; });
    var count = $("pending-count");
    count.hidden = !pending.length;
    count.textContent = pending.length;
    if (!actions.length) {
      panel.appendChild(el("p", "empty", "Nothing to approve yet. When the agent wants to change something, the request appears here."));
      return;
    }
    actions.forEach(function (a) {
      var card = el("article", "card card-" + a.status);
      var head = el("div", "card-head");
      head.appendChild(el("h3", "", ACTION_TITLES[a.tool] || a.tool));
      head.appendChild(el("span", "pill pill-" + a.status, a.status_label));
      card.appendChild(head);
      card.appendChild(paramList(a));
      if (a.status === "pending") {
        var buttons = el("div", "card-actions");
        var reject = el("button", "btn btn-quiet", "Reject");
        var approve = el("button", "btn", "Approve");
        reject.type = approve.type = "button";
        reject.addEventListener("click", function () { decide(a.id, "reject", card); });
        approve.addEventListener("click", function () { decide(a.id, "approve", card); });
        buttons.appendChild(reject);
        buttons.appendChild(approve);
        card.appendChild(buttons);
      } else if (a.result) {
        card.appendChild(el("p", "result", a.result));
      }
      panel.appendChild(card);
    });
  }

  function renderActivity(activity) {
    var panel = $("panel-activity");
    panel.textContent = "";
    if (!activity.length) {
      panel.appendChild(el("p", "empty", "Every tool the agent uses is logged here, with what it sent and what came back."));
      return;
    }
    var list = el("ol", "log");
    activity.forEach(function (c) {
      var li = el("li", c.is_error ? "log-error" : c.is_write ? "log-write" : "log-read");
      var head = el("div", "log-head");
      head.appendChild(el("strong", "", TOOL_LABELS[c.tool] || c.tool));
      head.appendChild(el("span", "log-time", c.at));
      li.appendChild(head);
      var details = el("details");
      details.appendChild(el("summary", "", c.is_error ? "Refused: see why" : "Input and result"));
      details.appendChild(el("pre", "", JSON.stringify(c.params, null, 1)));
      details.appendChild(el("pre", "", c.result));
      li.appendChild(details);
      list.appendChild(li);
    });
    panel.appendChild(list);
  }

  function table(columns, rows) {
    var t = el("table", "data");
    var head = el("tr");
    columns.forEach(function (c) { head.appendChild(el("th", c[2] || "", c[0])); });
    var thead = el("thead");
    thead.appendChild(head);
    t.appendChild(thead);
    var body = el("tbody");
    rows.forEach(function (row) {
      var tr = el("tr");
      columns.forEach(function (c) { tr.appendChild(el("td", c[2] || "", c[1](row))); });
      body.appendChild(tr);
    });
    t.appendChild(body);
    return t;
  }

  function renderData(data) {
    var panel = $("panel-data");
    panel.textContent = "";
    panel.appendChild(el("h3", "", "Orders"));
    panel.appendChild(table([
      ["Order", function (o) { return o.order_id; }],
      ["Customer", function (o) { return o.customer_name; }],
      ["Total", function (o) { return o.total_chf.toFixed(2); }, "num"],
      ["Refunded", function (o) { return o.refunded_chf ? o.refunded_chf.toFixed(2) : "-"; }, "num"],
      ["Status", function (o) { return o.status; }],
    ], data.orders));
    panel.appendChild(el("h3", "", "Upcoming appointments"));
    panel.appendChild(table([["When", function (e) { return e.start; }], ["What", function (e) { return e.title; }], ["Min", function (e) { return e.minutes; }, "num"]], data.events));
    panel.appendChild(el("h3", "", "Outbox"));
    if (!data.outbox.length) panel.appendChild(el("p", "empty", "No emails yet. Approved emails appear here; nothing is actually sent."));
    data.outbox.forEach(function (m) {
      var card = el("article", "card");
      card.appendChild(el("p", "mail-head", "To " + m.to + ": " + m.subject));
      card.appendChild(el("p", "body", m.body));
      panel.appendChild(card);
    });
    panel.appendChild(el("h3", "", "Customers"));
    panel.appendChild(table([["ID", function (c) { return c.customer_id; }], ["Name", function (c) { return c.name; }], ["City", function (c) { return c.city; }]], data.customers));
  }

  function render(state) {
    renderMessages(state.messages);
    renderQueue(state.actions);
    renderActivity(state.activity);
    renderData(state.data);
  }

  // ---- actions -----------------------------------------------------------------------------
  function decide(id, decision, card) {
    card.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    api("/api/actions/" + id + "/" + decision, {}).then(function (d) { render(d.state); }).catch(function (err) {
      card.appendChild(el("p", "result bad", err.message));
    });
  }

  function submit(payload, shownText) {
    if (busy) return;
    busy = true;
    send.disabled = true;
    document.querySelectorAll(".example").forEach(function (b) { b.disabled = true; });
    $("intro").hidden = true;
    var mine = el("div", "msg msg-user");
    mine.appendChild(el("p", "", shownText));
    thread.appendChild(mine);
    var thinking = el("div", "msg msg-assistant thinking");
    thinking.appendChild(el("p", "", "Working on it"));
    thread.appendChild(thinking);
    thread.scrollTop = thread.scrollHeight;
    api("/api/chat", payload)
      .then(function (d) {
        render(d.state);
        if (d.state.actions.some(function (a) { return a.status === "pending"; })) selectTab("queue");
      })
      .catch(function (err) {
        thinking.remove();
        var row = el("div", "msg msg-error");
        row.appendChild(el("p", "", err.message));
        thread.appendChild(row);
      })
      .then(function () {
        busy = false;
        send.disabled = false;
        document.querySelectorAll(".example").forEach(function (b) { b.disabled = false; });
      });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    submit({ text: text }, text);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  document.querySelectorAll(".example").forEach(function (btn) {
    btn.addEventListener("click", function () { submit({ script: btn.getAttribute("data-script") }, btn.textContent); });
  });
  $("reset").addEventListener("click", function () {
    api("/api/reset", {}).then(function (d) { render(d.state); selectTab("queue"); });
  });

  // ---- tabs --------------------------------------------------------------------------------
  function selectTab(name) {
    ["queue", "activity", "data"].forEach(function (key) {
      var on = key === name;
      $("tab-" + key).setAttribute("aria-selected", String(on));
      $("panel-" + key).hidden = !on;
    });
  }
  ["queue", "activity", "data"].forEach(function (key) {
    $("tab-" + key).addEventListener("click", function () { selectTab(key); });
  });

  api("/api/state").then(render).catch(function () {});
})();
