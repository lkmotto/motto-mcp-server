"""Cockpit HTML templates — single-page cockpit UI and director approval queue."""

from __future__ import annotations

from html import escape as h


def _render_cockpit(token: str) -> str:
    """Single-page cockpit UI. Token is embedded so the same browser session
    can call /cockpit/* JSON endpoints from JS without re-prompting.
    """
    safe_token = h(token)
    return f"""<!DOCTYPE html>
<html><head>
  <title>motto cockpit</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root {{
      --bg: #0e1116; --panel: #161b22; --border: #2d333b;
      --fg: #e6edf3; --muted: #7d8590; --accent: #2f81f7;
      --ok: #3fb950; --warn: #d29922; --err: #f85149;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg); color: var(--fg); font-size: 14px; min-height: 100vh;
    }}
    .top {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 16px; border-bottom: 1px solid var(--border); background: var(--panel);
    }}
    .top h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
    .top .meta {{ color: var(--muted); font-size: 12px; }}
    .layout {{
      display: grid; grid-template-columns: 1.2fr 1fr; gap: 12px;
      padding: 12px; height: calc(100vh - 49px);
    }}
    @media (max-width: 1000px) {{
      .layout {{ grid-template-columns: 1fr; height: auto; }}
    }}
    /* mobile: <=640px */
    @media (max-width: 640px) {{
      body {{ font-size: 15px; }}
      .top {{
        flex-wrap: wrap; gap: 4px; padding: 8px 12px;
      }}
      .top h1 {{ font-size: 15px; }}
      .top .meta {{ font-size: 11px; flex-basis: 100%; }}
      .layout {{
        padding: 8px; gap: 8px;
      }}
      .col {{ gap: 8px; }}
      .panel h2 {{
        padding: 8px 12px; font-size: 12px;
        flex-wrap: wrap; gap: 4px;
      }}
      .panel .body {{ padding: 10px 12px; }}
      /* chat fills viewport on mobile */
      .col:first-child .panel {{
        min-height: 60vh;
      }}
      #chat-log {{ padding: 10px 12px; }}
      .msg {{ max-width: 92%; font-size: 14px; }}
      #chat-form {{
        flex-direction: column; gap: 6px; padding: 8px;
      }}
      #chat-input {{
        font-size: 16px; /* prevents iOS zoom-on-focus */
        min-height: 44px;
      }}
      #chat-form button {{ width: 100%; padding: 10px 14px; font-size: 14px; }}
      /* intent form: stack to single column */
      #intent-form {{ grid-template-columns: 1fr; padding: 8px 12px !important; }}
      #intent-form input, #intent-form textarea {{ font-size: 16px; padding: 8px 10px; }}
      #intent-form .row-full {{ flex-wrap: wrap; gap: 6px; }}
      #intent-form button {{ flex: 1; min-width: 120px; padding: 10px 12px; }}
      .quick-btns {{ padding: 6px 12px 10px; }}
      .quick-btns button {{ flex: 1 1 calc(50% - 6px); font-size: 12px; padding: 8px 10px; }}
      /* local bridge form */
      #local-form {{ padding: 8px 12px !important; }}
      #local-form select, #local-form textarea, #local-form input {{ font-size: 16px !important; }}
      #local-form button {{ width: 100%; padding: 10px 14px; font-size: 14px; }}
      /* tables: horizontal scroll instead of squish */
      .panel .body table {{ display: block; overflow-x: auto; white-space: nowrap; }}
      th, td {{ padding: 6px 8px; }}
    }}
    /* very small (\u2264380px): tighten further */
    @media (max-width: 380px) {{
      .top h1 {{ font-size: 14px; }}
      .layout {{ padding: 6px; }}
      .panel h2 {{ font-size: 11px; padding: 7px 10px; }}
      .quick-btns button {{ flex-basis: 100%; }}
    }}
    .col {{ display: flex; flex-direction: column; gap: 12px; min-height: 0; }}
    .panel {{
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 6px; display: flex; flex-direction: column; min-height: 0;
    }}
    .panel h2 {{
      margin: 0; padding: 10px 14px; font-size: 13px; font-weight: 600;
      border-bottom: 1px solid var(--border); color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px;
      display: flex; justify-content: space-between; align-items: center;
    }}
    .panel .body {{ padding: 12px 14px; overflow-y: auto; flex: 1; min-height: 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{
      text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 600; font-size: 11px; }}
    code {{ font-family: ui-monospace, monospace; font-size: 11px; color: var(--fg); }}
    .ok {{ color: var(--ok); }}
    .warn {{ color: var(--warn); }}
    .err {{ color: var(--err); }}
    .accent {{ color: var(--accent); }}

    /* chat */
    #chat-log {{
      flex: 1; overflow-y: auto; padding: 12px 14px;
      display: flex; flex-direction: column; gap: 8px;
    }}
    .msg {{ padding: 8px 10px; border-radius: 6px; max-width: 85%; line-height: 1.45; white-space: pre-wrap; word-wrap: break-word; }}
    .msg.user {{ background: #1f6feb33; border: 1px solid #1f6feb55; align-self: flex-end; }}
    .msg.assistant {{ background: #161b22; border: 1px solid var(--border); align-self: flex-start; }}
    .msg.thinking {{ color: var(--muted); font-style: italic; }}
    .msg.error {{ background: #f8514922; border: 1px solid var(--err); color: var(--err); }}
    .msg.tool {{
      background: #0d1117; border: 1px dashed var(--border);
      align-self: stretch; max-width: 100%; padding: 6px 10px;
      font-size: 12px; color: var(--fg); white-space: normal;
    }}
    .msg.tool.tool-ok .tool-ico {{ color: var(--ok); font-weight: 700; }}
    .msg.tool.tool-err .tool-ico {{ color: var(--err); font-weight: 700; }}
    .msg.tool .tool-head code {{ background: #1f2937; padding: 1px 5px; border-radius: 3px; }}
    .msg.tool .tool-args {{ color: var(--muted); font-family: ui-monospace, monospace; font-size: 11px; }}
    .msg.tool .tool-summary {{ margin-top: 4px; color: var(--muted); }}
    .msg.tool .tool-summary b {{ color: var(--accent); }}
    .msg.tool details.tool-raw {{ margin-top: 4px; }}
    .msg.tool details.tool-raw summary {{ cursor: pointer; color: var(--muted); font-size: 11px; }}
    .msg.tool details.tool-raw pre {{
      background: #010409; border: 1px solid var(--border); border-radius: 4px;
      padding: 6px 8px; overflow-x: auto; font-size: 11px; color: var(--fg);
      max-height: 240px;
    }}
    #chat-form {{
      display: flex; gap: 8px; padding: 10px; border-top: 1px solid var(--border);
    }}
    #chat-input {{
      flex: 1; padding: 8px 10px; background: var(--bg); border: 1px solid var(--border);
      border-radius: 4px; color: var(--fg); font-size: 13px; resize: vertical; min-height: 38px; max-height: 120px;
      font-family: inherit;
    }}
    button {{
      background: var(--accent); color: white; border: 0; padding: 8px 14px;
      border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 500;
    }}
    button:hover {{ background: #4493f8; }}
    button:disabled {{ background: #555; cursor: not-allowed; }}
    button.ghost {{ background: transparent; color: var(--accent); border: 1px solid var(--accent); }}
    button.ghost:hover {{ background: #2f81f722; }}

    /* intent form */
    #intent-form {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
    #intent-form input, #intent-form textarea {{
      background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
      padding: 6px 8px; color: var(--fg); font-size: 12px; font-family: inherit;
    }}
    #intent-form textarea {{ grid-column: 1 / -1; min-height: 60px; resize: vertical; }}
    #intent-form .row-full {{ grid-column: 1 / -1; display: flex; gap: 8px; align-items: center; }}
    .quick-btns {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 14px 12px; }}
    .quick-btns button {{ font-size: 11px; padding: 4px 10px; }}
    #intent-result {{ font-size: 12px; padding: 0 14px 12px; }}
  </style>
</head><body>
  <div class="top">
    <h1>\U0001f6f0\ufe0f motto cockpit</h1>
    <span class="meta" id="status-line">connecting\u2026</span>
  </div>

  <div class="layout">
    <!-- Left: chat -->
    <div class="col">
      <div class="panel" style="flex:1">
        <h2>director chat <span style="font-weight:400;font-size:11px">deepseek \u00b7 v4-flash</span></h2>
        <div id="chat-log"></div>
        <form id="chat-form">
          <textarea id="chat-input" placeholder="ask the director\u2026 (enter to send, shift+enter for newline)"></textarea>
          <button type="submit" id="chat-send">send</button>
        </form>
      </div>
    </div>

    <!-- Right: fleet + intent -->
    <div class="col">
      <div class="panel">
        <h2>fleet <span id="agent-count" style="font-weight:400">\u2014</span></h2>
        <div class="body" id="agents-body">loading\u2026</div>
      </div>

      <div class="panel">
        <h2>send intent <span style="font-weight:400;font-size:11px">queues a manual nudge for an agent</span></h2>
        <form id="intent-form" style="padding:10px 14px;">
          <input id="i-target" placeholder="target_agent (e.g. motto-director)" required>
          <input id="i-kind" placeholder="kind (e.g. focus, halt, retry-pr)" required>
          <textarea id="i-payload" placeholder='payload JSON (e.g. {{"pr":42,"reason":"flaky test"}})'></textarea>
          <div class="row-full">
            <button type="submit">queue intent</button>
            <button type="button" class="ghost" id="i-clear">clear</button>
            <span id="intent-result"></span>
          </div>
        </form>
        <div class="quick-btns">
          <button class="ghost" data-target="motto-director" data-kind="poll-now">poll director now</button>
          <button class="ghost" data-target="motto-director" data-kind="merge-greenlit">merge greenlit PRs</button>
          <button class="ghost" data-target="motto-sdr-agent" data-kind="dry-run">SDR dry-run</button>
        </div>
      </div>

      <div class="panel">
        <h2>local bridge <span id="local-status" style="font-weight:400;font-size:11px">no runner detected</span></h2>
        <form id="local-form" style="padding:10px 14px;">
          <select id="l-kind" style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--fg);font-size:12px;width:100%;margin-bottom:6px;">
            <option value="echo">echo \u00b7 sanity ping</option>
            <option value="shell">shell \u00b7 run a command</option>
            <option value="read_file">read_file \u00b7 path</option>
            <option value="write_file">write_file \u00b7 path + content</option>
            <option value="screenshot">screenshot \u00b7 capture screen</option>
            <option value="ocr">ocr \u00b7 path \u2192 text</option>
            <option value="claude_code">claude_code \u00b7 prompt</option>
            <option value="browser">browser \u00b7 url + action</option>
          </select>
          <textarea id="l-payload" placeholder='payload JSON (e.g. {{"cmd":"ls -la"}})' style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--fg);font-size:12px;width:100%;min-height:50px;font-family:inherit;"></textarea>
          <div style="display:flex;gap:8px;align-items:center;margin-top:6px;">
            <button type="submit">queue local task</button>
            <span id="local-result" style="font-size:12px;"></span>
          </div>
        </form>
        <div class="body" id="local-body" style="max-height:240px">no tasks yet</div>
      </div>

      <div class="panel" style="flex:1">
        <h2>recent events <span style="font-weight:400;font-size:11px">last 60 min</span></h2>
        <div class="body" id="events-body">loading\u2026</div>
      </div>
    </div>
  </div>

<script>
const TOKEN = "{safe_token}";
const Q = TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "";

let chatHistory = [];

function fmtAge(ts) {{
  if (!ts) return "\u2014";
  const dt = new Date(ts);
  const s = (Date.now() - dt.getTime()) / 1000;
  if (s < 0) return "now";
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s/60) + "m";
  if (s < 86400) return Math.floor(s/3600) + "h";
  return Math.floor(s/86400) + "d";
}}

function statusColor(s) {{
  if (s === "success") return "ok";
  if (s === "error") return "err";
  if (s === "running") return "accent";
  return "";
}}

async function refreshState() {{
  try {{
    const r = await fetch("/cockpit/state.json" + Q);
    if (!r.ok) {{ document.getElementById("status-line").textContent = "auth error"; return; }}
    const d = await r.json();

    // Status line
    document.getElementById("status-line").textContent =
      d.agents.length + " agents \u00b7 " + d.recent_events.length + " events \u00b7 updated " + new Date().toLocaleTimeString();
    document.getElementById("agent-count").textContent = d.agents.length;

    // Agents table
    const aBody = document.getElementById("agents-body");
    if (!d.agents.length) {{
      aBody.innerHTML = "<i>no agents registered</i>";
    }} else {{
      let html = "<table><tr><th>agent</th><th>kind</th><th>last seen</th><th>last run</th><th>open</th></tr>";
      for (const a of d.agents) {{
        const lr = a.last_run || {{}};
        html += "<tr>" +
          "<td><b>" + escapeHtml(a.name) + "</b></td>" +
          "<td>" + escapeHtml(a.kind) + "</td>" +
          "<td title='" + escapeHtml(a.last_seen_at || "") + "'>" + fmtAge(a.last_seen_at) + "</td>" +
          "<td><span class='" + statusColor(lr.status) + "'>" + escapeHtml(lr.kind || "\u2014") + " " + escapeHtml(lr.status || "") + "</span></td>" +
          "<td>" + (a.open_intents || 0) + "</td>" +
          "</tr>";
      }}
      html += "</table>";
      aBody.innerHTML = html;
    }}

    // Events
    const eBody = document.getElementById("events-body");
    if (!d.recent_events.length) {{
      eBody.innerHTML = "<i>no events</i>";
    }} else {{
      let html = "<table><tr><th>when</th><th>agent</th><th>kind</th><th>payload</th></tr>";
      for (const e of d.recent_events.slice(0, 30)) {{
        const p = JSON.stringify(e.payload || {{}});
        const pShort = p.length > 100 ? p.slice(0, 97) + "\u2026" : p;
        html += "<tr>" +
          "<td title='" + escapeHtml(e.ts || "") + "'>" + fmtAge(e.ts) + "</td>" +
          "<td>" + escapeHtml(e.agent_name || "\u2014") + "</td>" +
          "<td><code>" + escapeHtml(e.kind || "\u2014") + "</code></td>" +
          "<td><code>" + escapeHtml(pShort) + "</code></td>" +
          "</tr>";
      }}
      html += "</table>";
      eBody.innerHTML = html;
    }}
  }} catch (err) {{
    document.getElementById("status-line").textContent = "error: " + err.message;
  }}
}}

function escapeHtml(s) {{
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({{
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }})[c]);
}}

function addChatMsg(role, text, cls) {{
  const log = document.getElementById("chat-log");
  const div = document.createElement("div");
  div.className = "msg " + role + (cls ? " " + cls : "");
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}}

function addToolCallMsg(tc) {{
  // tc = {{name, arguments, result, ok, hop}}
  const log = document.getElementById("chat-log");
  const wrap = document.createElement("div");
  wrap.className = "msg tool" + (tc.ok ? " tool-ok" : " tool-err");
  const head = document.createElement("div");
  head.className = "tool-head";
  const ico = tc.ok ? "\u2713" : "\u26a0";
  head.innerHTML = "<span class='tool-ico'>" + ico + "</span>" +
    " <code>" + escapeHtml(tc.name) + "</code> " +
    "<span class='tool-args'>" + escapeHtml(JSON.stringify(tc.arguments || {{}})) + "</span>";
  wrap.appendChild(head);
  // Highlight queued move IDs / request IDs so they're glanceable.
  const r = tc.result || {{}};
  const summary = document.createElement("div");
  summary.className = "tool-summary";
  if (r.queued_move_id) {{
    summary.innerHTML = "queued move <b>#" + r.queued_move_id + "</b>" +
      " (" + escapeHtml(r.kind || "?") + ", " + escapeHtml(r.status || "pending") + ")" +
      " \u2014 approve in queue panel";
  }} else if (r.request_id) {{
    summary.innerHTML = "capability request <b>#" + r.request_id + "</b>" +
      " (" + escapeHtml(r.status || "pending") + ")";
  }} else if (r.error) {{
    summary.innerHTML = "<span class='err'>" + escapeHtml(r.error) + "</span>";
  }} else if (typeof r.count === "number") {{
    summary.textContent = r.count + " rows";
  }} else {{
    summary.textContent = "ok";
  }}
  wrap.appendChild(summary);
  // Collapsible raw JSON for inspection.
  const det = document.createElement("details");
  det.className = "tool-raw";
  const sum = document.createElement("summary");
  sum.textContent = "raw";
  det.appendChild(sum);
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(r, null, 2);
  det.appendChild(pre);
  wrap.appendChild(det);
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
}}

document.getElementById("chat-form").addEventListener("submit", async (ev) => {{
  ev.preventDefault();
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("chat-send");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addChatMsg("user", text);
  chatHistory.push({{role:"user", content:text}});
  const thinking = addChatMsg("assistant", "thinking\u2026", "thinking");
  sendBtn.disabled = true;
  try {{
    const r = await fetch("/cockpit/chat" + Q, {{
      method: "POST",
      headers: {{"content-type":"application/json"}},
      body: JSON.stringify({{messages: chatHistory}})
    }});
    const d = await r.json();
    thinking.remove();
    // Render any tool calls inline first so the UI matches the order
    // of work \u2014 tool actions appear, then the assistant's recap.
    if (Array.isArray(d.tool_calls)) {{
      for (const tc of d.tool_calls) {{
        addToolCallMsg(tc);
      }}
      // If a propose_* tool fired, refresh the pending approvals panel
      // so the new row appears without a manual reload.
      const filed = d.tool_calls.some(t =>
        t.ok && t.result && t.result.queued_move_id);
      if (filed && typeof refreshDirectorPending === "function") {{
        refreshDirectorPending();
      }}
    }}
    if (d.error || (d.reply && d.reply.startsWith("[error"))) {{
      const msg = d.reply || ("error: " + JSON.stringify(d.error));
      addChatMsg("assistant", msg, "error");
      // don't push errors to history
    }} else {{
      addChatMsg("assistant", d.reply);
      chatHistory.push({{role:"assistant", content:d.reply}});
    }}
  }} catch (err) {{
    thinking.remove();
    addChatMsg("assistant", "transport error: " + err.message, "error");
  }}
  sendBtn.disabled = false;
  input.focus();
}});

// Enter to send, Shift+Enter for newline
document.getElementById("chat-input").addEventListener("keydown", (ev) => {{
  if (ev.key === "Enter" && !ev.shiftKey) {{
    ev.preventDefault();
    document.getElementById("chat-form").dispatchEvent(new Event("submit"));
  }}
}});

// Intent form
document.getElementById("intent-form").addEventListener("submit", async (ev) => {{
  ev.preventDefault();
  const target = document.getElementById("i-target").value.trim();
  const kind = document.getElementById("i-kind").value.trim();
  const payloadRaw = document.getElementById("i-payload").value.trim();
  let payload = {{}};
  if (payloadRaw) {{
    try {{ payload = JSON.parse(payloadRaw); }}
    catch {{
      document.getElementById("intent-result").innerHTML = "<span class='err'>invalid JSON in payload</span>";
      return;
    }}
  }}
  document.getElementById("intent-result").textContent = "submitting\u2026";
  try {{
    const r = await fetch("/cockpit/intent" + Q, {{
      method: "POST",
      headers: {{"content-type":"application/json"}},
      body: JSON.stringify({{target_agent: target, kind, payload}})
    }});
    const d = await r.json();
    if (d.ok) {{
      document.getElementById("intent-result").innerHTML =
        "<span class='ok'>queued \u00b7 " + escapeHtml(d.intent_id.slice(0,8)) + "</span>";
      document.getElementById("i-payload").value = "";
      refreshState();
    }} else {{
      document.getElementById("intent-result").innerHTML =
        "<span class='err'>" + escapeHtml(d.error || "failed") + "</span>";
    }}
  }} catch (err) {{
    document.getElementById("intent-result").innerHTML =
      "<span class='err'>" + escapeHtml(err.message) + "</span>";
  }}
}});

document.getElementById("i-clear").addEventListener("click", () => {{
  document.getElementById("i-target").value = "";
  document.getElementById("i-kind").value = "";
  document.getElementById("i-payload").value = "";
  document.getElementById("intent-result").textContent = "";
}});

// Quick action buttons
document.querySelectorAll(".quick-btns button").forEach(btn => {{
  btn.addEventListener("click", () => {{
    document.getElementById("i-target").value = btn.dataset.target;
    document.getElementById("i-kind").value = btn.dataset.kind;
    document.getElementById("i-payload").value = "";
    document.getElementById("i-target").scrollIntoView({{behavior:"smooth"}});
  }});
}});

// \u2500\u2500 Local bridge \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
const LOCAL_PAYLOAD_HINTS = {{
  echo: '{{"msg":"hello from cockpit"}}',
  shell: '{{"cmd":"ls -la","cwd":"~"}}',
  read_file: '{{"path":"/Users/luke/something.txt"}}',
  write_file: '{{"path":"/tmp/test.txt","content":"hello"}}',
  screenshot: '{{}}',
  ocr: '{{"path":"/tmp/screenshot.png"}}',
  claude_code: '{{"prompt":"review the comp picks in this appraisal","cwd":"~/projects/motto"}}',
  browser: '{{"url":"https://example.com","action":"screenshot"}}'
}};

document.getElementById("l-kind").addEventListener("change", (ev) => {{
  const ta = document.getElementById("l-payload");
  if (!ta.value.trim()) ta.value = LOCAL_PAYLOAD_HINTS[ev.target.value] || "{{}}";
}});
document.getElementById("l-payload").value = LOCAL_PAYLOAD_HINTS.echo;

async function refreshLocal() {{
  try {{
    const r = await fetch("/local/tasks.json" + (Q ? Q + "&" : "?") + "limit=15");
    if (!r.ok) return;
    const d = await r.json();
    const body = document.getElementById("local-body");
    const status = document.getElementById("local-status");
    if (!d.tasks || !d.tasks.length) {{
      body.innerHTML = "<i>no tasks yet</i>";
      status.textContent = "queue empty";
      return;
    }}
    const claimed = d.tasks.filter(t => t.claimed_by).map(t => t.claimed_by);
    const runners = [...new Set(claimed)];
    status.textContent = runners.length ? ("runner: " + runners.join(", ")) : "no runner has claimed yet";
    let html = "<table><tr><th>when</th><th>kind</th><th>status</th><th>desc</th></tr>";
    for (const t of d.tasks) {{
      const cls = t.status === "succeeded" ? "ok" : (t.status === "failed" ? "err" : (t.status === "running" || t.status === "claimed" ? "accent" : ""));
      const desc = t.description || (t.error ? t.error.slice(0, 60) : (t.kind + (t.claimed_by ? " \u2192 " + t.claimed_by : "")));
      html += "<tr>" +
        "<td title='" + escapeHtml(t.created_at || "") + "'>" + fmtAge(t.created_at) + "</td>" +
        "<td><code>" + escapeHtml(t.kind) + "</code></td>" +
        "<td><span class='" + cls + "'>" + escapeHtml(t.status) + "</span></td>" +
        "<td><code title='" + escapeHtml(t.id || "") + "'>" + escapeHtml(desc) + "</code></td>" +
        "</tr>";
    }}
    html += "</table>";
    body.innerHTML = html;
  }} catch (err) {{
    // silent
  }}
}}

document.getElementById("local-form").addEventListener("submit", async (ev) => {{
  ev.preventDefault();
  const kind = document.getElementById("l-kind").value;
  const payloadRaw = document.getElementById("l-payload").value.trim();
  let payload = {{}};
  if (payloadRaw) {{
    try {{ payload = JSON.parse(payloadRaw); }}
    catch {{
      document.getElementById("local-result").innerHTML = "<span class='err'>invalid JSON</span>";
      return;
    }}
  }}
  document.getElementById("local-result").textContent = "queueing\u2026";
  try {{
    const r = await fetch("/local/queue" + Q, {{
      method: "POST",
      headers: {{"content-type":"application/json"}},
      body: JSON.stringify({{kind, payload, source: "cockpit-user"}})
    }});
    const d = await r.json();
    if (d.id) {{
      document.getElementById("local-result").innerHTML = "<span class='ok'>queued \u00b7 " + escapeHtml(d.id.slice(0,8)) + "</span>";
      refreshLocal();
    }} else {{
      document.getElementById("local-result").innerHTML = "<span class='err'>" + escapeHtml(d.error || "failed") + "</span>";
    }}
  }} catch (err) {{
    document.getElementById("local-result").innerHTML = "<span class='err'>" + escapeHtml(err.message) + "</span>";
  }}
}});

// Initial load + poll
refreshState();
refreshLocal();
setInterval(refreshState, 15000);
setInterval(refreshLocal, 5000);

// Welcome
addChatMsg("assistant", "I'm the Motto Director. I can see live fleet state in my context. Ask me what's happening, what to do next, or describe a nudge you want to send.");
</script>
</body></html>"""


def _render_director(token: str) -> str:
    """Director approval queue UI \u2014 list pending moves, approve/reject, bulk."""
    safe_token = h(token)
    return f"""<!DOCTYPE html>
<html><head>
  <title>motto director \u00b7 approvals</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root {{
      --bg: #0e1116; --panel: #161b22; --border: #2d333b;
      --fg: #e6edf3; --muted: #7d8590; --accent: #2f81f7;
      --ok: #3fb950; --warn: #d29922; --err: #f85149;
      --kind-issue: #d29922; --kind-spawn: #2f81f7;
      --kind-merge: #3fb950; --kind-nudge: #a371f7;
      --kind-compound: #f78166;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg); color: var(--fg); font-size: 14px; min-height: 100vh;
    }}
    .top {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 16px; border-bottom: 1px solid var(--border); background: var(--panel);
      flex-wrap: wrap; gap: 8px;
    }}
    .top h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
    .top a {{ color: var(--accent); text-decoration: none; font-size: 12px; }}
    .top .meta {{ color: var(--muted); font-size: 12px; }}
    .toolbar {{
      display: flex; gap: 8px; padding: 10px 16px;
      border-bottom: 1px solid var(--border); background: var(--panel);
      flex-wrap: wrap; align-items: center;
    }}
    .toolbar select, .toolbar button {{
      background: var(--bg); color: var(--fg); border: 1px solid var(--border);
      padding: 6px 12px; border-radius: 6px; font-size: 13px; cursor: pointer;
    }}
    .toolbar button:hover {{ border-color: var(--accent); }}
    .toolbar .counts {{ color: var(--muted); font-size: 12px; margin-left: auto; }}
    .toolbar .counts span {{ margin-left: 10px; }}
    .toolbar .counts .pending {{ color: var(--warn); }}
    .toolbar .counts .approved {{ color: var(--accent); }}
    .toolbar .counts .applied {{ color: var(--ok); }}
    .toolbar .counts .rejected {{ color: var(--err); }}
    .list {{ padding: 12px; display: flex; flex-direction: column; gap: 10px; }}
    .move {{
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 12px; display: flex; gap: 12px;
      align-items: flex-start;
    }}
    .move .check {{ flex-shrink: 0; margin-top: 4px; }}
    .move .body {{ flex: 1; min-width: 0; }}
    .move .head {{
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
      margin-bottom: 6px;
    }}
    .move .head .priority {{
      background: #21262d; color: var(--muted); padding: 2px 6px;
      border-radius: 4px; font-size: 11px; font-family: ui-monospace, monospace;
    }}
    .move .head .priority.p-high {{ background: #6e1c1c; color: #ffeded; }}
    .move .head .priority.p-med  {{ background: #5a3a09; color: #ffe5b4; }}
    .move .head .repo {{
      color: var(--muted); font-size: 12px; font-family: ui-monospace, monospace;
    }}
    .move .head .kind {{
      padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.4px; color: #fff;
    }}
    .move .head .kind.file_issue {{ background: var(--kind-issue); }}
    .move .head .kind.spawn_session {{ background: var(--kind-spawn); }}
    .move .head .kind.merge_pr {{ background: var(--kind-merge); }}
    .move .head .kind.nudge_pipeline {{ background: var(--kind-nudge); }}
    .move .head .kind.compound_pr {{ background: var(--kind-compound); }}
    .move .head .kind.noop {{ background: var(--muted); }}
    .move .title {{
      font-weight: 600; font-size: 14px; margin: 0 0 4px;
      word-break: break-word;
    }}
    .move .rationale {{
      color: var(--muted); font-size: 13px; line-height: 1.45;
      word-break: break-word;
    }}
    .move .rationale.long {{
      max-height: 4.5em; overflow: hidden; position: relative;
    }}
    .move .rationale.expanded {{ max-height: none; }}
    .move .show-more {{
      color: var(--accent); font-size: 12px; cursor: pointer;
      background: none; border: none; padding: 0; margin-top: 4px;
    }}
    .move .footer {{
      margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap;
      align-items: center;
    }}
    .move .footer .meta {{
      color: var(--muted); font-size: 11px; margin-right: auto;
      font-family: ui-monospace, monospace;
    }}
    .move .footer button {{
      background: var(--bg); color: var(--fg); border: 1px solid var(--border);
      padding: 5px 12px; border-radius: 6px; font-size: 12px; cursor: pointer;
      font-weight: 500;
    }}
    .move .footer button.approve {{ border-color: var(--ok); color: var(--ok); }}
    .move .footer button.approve:hover {{ background: var(--ok); color: #fff; }}
    .move .footer button.reject {{ border-color: var(--err); color: var(--err); }}
    .move .footer button.reject:hover {{ background: var(--err); color: #fff; }}
    .move .footer button:disabled {{
      opacity: 0.5; cursor: not-allowed;
    }}
    .empty {{
      padding: 40px; text-align: center; color: var(--muted);
    }}
    .err-banner {{
      background: #3d1d1d; color: var(--err); padding: 10px 16px;
      border-bottom: 1px solid var(--err); font-size: 13px;
    }}
    /* mobile */
    @media (max-width: 640px) {{
      body {{ font-size: 15px; }}
      .top {{ padding: 8px 12px; }}
      .toolbar {{ padding: 8px 12px; gap: 6px; }}
      .toolbar .counts {{ flex-basis: 100%; margin-left: 0; }}
      .toolbar .counts span {{ margin-left: 0; margin-right: 10px; }}
      .toolbar select, .toolbar button {{ font-size: 13px; padding: 8px 12px; }}
      .list {{ padding: 8px; }}
      .move {{ padding: 10px; }}
      .move .footer {{ gap: 4px; }}
      .move .footer .meta {{ flex-basis: 100%; margin-right: 0; }}
      .move .footer button {{ flex: 1; padding: 8px; }}
    }}
  </style>
</head><body>
  <div class="top">
    <div>
      <h1>director \u00b7 approvals</h1>
      <div class="meta">pending moves awaiting human review</div>
    </div>
    <div><a href="/cockpit?token={safe_token}">\u2190 back to cockpit</a></div>
  </div>
  <div class="toolbar">
    <select id="status-filter">
      <option value="pending" selected>pending</option>
      <option value="approved">approved</option>
      <option value="applied">applied</option>
      <option value="rejected">rejected</option>
      <option value="failed">failed</option>
      <option value="expired">expired</option>
    </select>
    <button id="refresh-btn">refresh</button>
    <button id="approve-all-btn">approve all visible</button>
    <span class="counts" id="counts"></span>
  </div>
  <div id="err-banner"></div>
  <div class="list" id="moves-list">
    <div class="empty">loading\u2026</div>
  </div>
<script>
const Q = "?token=" + encodeURIComponent("{safe_token}");

function escapeHtml(s) {{
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}}

function priorityClass(p) {{
  if (p >= 8) return "p-high";
  if (p >= 5) return "p-med";
  return "";
}}

function renderCounts(c) {{
  const el = document.getElementById("counts");
  if (!c) {{ el.textContent = ""; return; }}
  const parts = [];
  for (const k of ["pending","approved","applied","rejected","failed","expired"]) {{
    if (c[k]) parts.push('<span class="' + k + '">' + k + ': ' + c[k] + '</span>');
  }}
  el.innerHTML = parts.join("");
}}

function renderMove(m) {{
  const pCls = priorityClass(m.priority || 0);
  const isPending = m.status === "pending";
  const rationale = m.rationale || "";
  const long = rationale.length > 200;
  const meta = (m.created_at ? m.created_at.replace("T"," ").slice(0,16) + " \u00b7 " : "")
             + "id " + m.id
             + (m.run_id ? " \u00b7 run " + String(m.run_id).slice(0,8) : "");
  const approveDisabled = !isPending ? "disabled" : "";
  const rejectDisabled = !isPending ? "disabled" : "";
  return `
    <div class="move" data-id="${{m.id}}">
      ${{isPending ? '<input type="checkbox" class="check" data-id="' + m.id + '">' : ''}}
      <div class="body">
        <div class="head">
          <span class="priority ${{pCls}}">P${{m.priority || 0}}</span>
          <span class="kind ${{escapeHtml(m.kind)}}">${{escapeHtml(m.kind)}}</span>
          <span class="repo">${{escapeHtml(m.repo)}}</span>
        </div>
        <div class="title">${{escapeHtml(m.title)}}</div>
        <div class="rationale ${{long ? 'long' : ''}}">${{escapeHtml(rationale)}}</div>
        ${{long ? '<button class="show-more" data-id="' + m.id + '">show more</button>' : ''}}
        <div class="footer">
          <span class="meta">${{escapeHtml(meta)}}${{"
            m.approved_by ? ' \u00b7 by ' + escapeHtml(m.approved_by) : ''
          }}</span>
          <button class="approve" data-id="${{m.id}}" ${{approveDisabled}}>approve</button>
          <button class="reject" data-id="${{m.id}}" ${{rejectDisabled}}>reject</button>
        </div>
      </div>
    </div>`;
}}

async function refresh() {{
  const status = document.getElementById("status-filter").value;
  const list = document.getElementById("moves-list");
  const banner = document.getElementById("err-banner");
  banner.innerHTML = "";
  try {{
    const url = "/cockpit/director/pending.json" + Q
              + "&status=" + encodeURIComponent(status);
    const r = await fetch(url, {{cache: "no-store"}});
    const d = await r.json();
    if (d.error) {{
      banner.innerHTML = '<div class="err-banner">' + escapeHtml(d.error) + '</div>';
      list.innerHTML = '<div class="empty">error</div>';
      return;
    }}
    renderCounts(d.counts);
    if (!d.moves || !d.moves.length) {{
      list.innerHTML = '<div class="empty">no ' + escapeHtml(status) + ' moves</div>';
      return;
    }}
    list.innerHTML = d.moves.map(renderMove).join("");
  }} catch (err) {{
    banner.innerHTML = '<div class="err-banner">' + escapeHtml(err.message) + '</div>';
  }}
}}

async function approveOne(id) {{
  const r = await fetch("/cockpit/director/approve" + Q, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{move_id: parseInt(id, 10)}})
  }});
  const d = await r.json();
  if (d.error) alert(d.error);
  refresh();
}}

async function rejectOne(id) {{
  if (!confirm("Reject move #" + id + "?")) return;
  const r = await fetch("/cockpit/director/reject" + Q, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{move_id: parseInt(id, 10)}})
  }});
  const d = await r.json();
  if (d.error) alert(d.error);
  refresh();
}}

async function approveAllVisible() {{
  const ids = Array.from(document.querySelectorAll('.move .check'))
    .map(el => parseInt(el.dataset.id, 10))
    .filter(Number.isFinite);
  if (!ids.length) return;
  if (!confirm("Approve " + ids.length + " moves?")) return;
  const r = await fetch("/cockpit/director/approve" + Q, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{move_ids: ids}})
  }});
  const d = await r.json();
  if (d.error) alert(d.error); else alert("approved " + d.approved);
  refresh();
}}

document.addEventListener("click", (ev) => {{
  const t = ev.target;
  if (!t || !t.dataset || !t.dataset.id) return;
  if (t.classList.contains("approve")) approveOne(t.dataset.id);
  else if (t.classList.contains("reject")) rejectOne(t.dataset.id);
  else if (t.classList.contains("show-more")) {{
    const move = t.closest(".move");
    if (move) {{
      const ra = move.querySelector(".rationale");
      if (ra) {{
        ra.classList.toggle("expanded");
        const expanded = ra.classList.contains("expanded");
        t.textContent = expanded ? "show less" : "show more";
      }}
    }}
  }}
}});

document.getElementById("refresh-btn").addEventListener("click", refresh);
document.getElementById("approve-all-btn").addEventListener("click", approveAllVisible);
document.getElementById("status-filter").addEventListener("change", refresh);

refresh();
setInterval(refresh, 15000);
</script>
</body></html>"""
