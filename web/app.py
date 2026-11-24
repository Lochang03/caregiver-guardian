import re
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

sys.path.append(str(Path(__file__).resolve().parent.parent / "bot"))
from mcp_client import call_tool  # noqa: E402

URL_RE = re.compile(r"(https?://\S+|www\.\S+)")
UPI_RE = re.compile(r"\b[\w.\-]{2,}@[a-zA-Z]{2,}\b")

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caregiver Guardian</title>
<style>
  body { font-family: system-ui, sans-serif; background: #f5f6f8; color: #1d2330; margin: 0; }
  main { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { margin-bottom: 4px; }
  h2 { font-size: 1.05rem; margin: 0 0 12px; }
  section { background: #fff; border-radius: 10px; padding: 16px; margin-top: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  textarea { width: 100%; box-sizing: border-box; font: inherit; padding: 10px; border: 1px solid #c9ced8; border-radius: 8px; }
  button { font: inherit; padding: 8px 14px; border: 0; border-radius: 8px; background: #2f5bd8; color: #fff; cursor: pointer; }
  button.ghost { background: #e6e9f0; color: #1d2330; }
  button:disabled { opacity: .5; cursor: default; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .muted, .note { color: #5b6473; font-size: .9rem; }
  .badge { display: inline-block; padding: 6px 12px; border-radius: 8px; font-weight: 600; }
  .badge.high { background: #fde2e2; color: #9b1c1c; }
  .badge.medium { background: #fff0d1; color: #8a5a00; }
  .badge.low { background: #dff5e3; color: #1b6b32; }
  .badge.unknown { background: #e6e9f0; }
  .card { border: 1px solid #d8dce5; border-radius: 8px; padding: 12px; margin-top: 10px; }
  pre { background: #f0f2f6; padding: 10px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; }
</style></head>
<body><main>
<h1>Caregiver Guardian</h1>
<p class="note">Prototype. All data is synthetic. No real payments or messages are sent.</p>

<section>
  <h2>1. Message the parent received</h2>
  <textarea id="msg" rows="4" placeholder="Paste a suspicious message here"></textarea>
  <div class="row">
    <button id="check">Check message</button>
    <button class="ghost" id="sample">Use sample scam</button>
  </div>
  <div id="verdict"></div>
</section>

<section>
  <h2>2. Proposed actions (need approval)</h2>
  <div id="cards"><p class="muted">Nothing proposed yet.</p></div>
</section>

<section>
  <h2>3. Audit log</h2>
  <button class="ghost" id="refresh">Refresh</button>
  <pre id="history">Loading...</pre>
</section>
</main>
<script>
const $ = (id) => document.getElementById(id);

async function post(url, body) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  return r.json();
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function showVerdict(v) {
  const box = $('verdict');
  box.replaceChildren();
  const labels = {
    high: 'HIGH RISK. Do not pay or share anything.',
    medium: 'Be careful. This looks suspicious.',
    low: 'Looks low risk.',
    unknown: 'Could not check this message.'
  };
  box.append(el('p'));
  box.append(el('div', labels[v.risk_level] || v.risk_level, 'badge ' + v.risk_level));
  box.append(el('p', 'Score: ' + v.score + '/100'));
  if (v.reasons.length) {
    const ul = el('ul');
    v.reasons.forEach((r) => ul.append(el('li', r)));
    box.append(ul);
  }
}

function addCard(p) {
  const card = el('div', undefined, 'card');
  card.append(el('strong', p.what_will_happen));
  card.append(el('p', 'Risk: ' + p.risk_level + '. Why: ' + p.reasons.join(', ')));
  card.append(el('p', 'Needs approval from: ' + p.needs_approval_from + '. If nobody answers in time it is cancelled, not run.', 'muted'));
  const msg = el('p', '');
  const row = el('div', undefined, 'row');
  [['Approve as parent', 'approve', 'parent'], ['Approve as child', 'approve', 'child'], ['Deny', 'deny', 'child']].forEach(([label, decision, role]) => {
    const b = el('button', label, decision === 'deny' ? 'ghost' : '');
    b.onclick = async () => {
      row.querySelectorAll('button').forEach((x) => (x.disabled = true));
      try {
        const d = await post('/api/decide', {action_id: p.action_id, role: role, decision: decision});
        if (d.error) {
          msg.textContent = 'Not allowed: ' + d.error;
          row.querySelectorAll('button').forEach((x) => (x.disabled = false));
        } else if (d.status === 'executed') {
          msg.textContent = 'Done: ' + d.result + ' (' + (d.verified ? 'verified' : 'NOT verified') + ')';
          row.remove();
        } else {
          msg.textContent = d.status === 'denied' ? 'Denied. Nothing was run.' : 'Expired. Cancelled, not run.';
          row.remove();
        }
      } catch (e) {
        msg.textContent = 'Something went wrong. Nothing was run.';
      }
      loadHistory();
    };
    row.append(b);
  });
  card.append(row, msg);
  $('cards').append(card);
}

async function check() {
  const message = $('msg').value.trim();
  if (!message) return;
  $('check').disabled = true;
  $('verdict').textContent = 'Checking...';
  $('cards').replaceChildren();
  try {
    const d = await post('/api/check', {message: message});
    if (d.error) {
      $('verdict').textContent = d.error;
    } else {
      showVerdict(d.verdict);
      if (d.actions.length) d.actions.forEach(addCard);
      else $('cards').append(el('p', 'No action needed.', 'muted'));
    }
  } catch (e) {
    $('verdict').textContent = 'Something went wrong. Nothing was run.';
  }
  $('check').disabled = false;
  loadHistory();
}

async function loadHistory() {
  try {
    const r = await fetch('/api/history');
    const d = await r.json();
    const rows = d.rows || [];
    $('history').textContent = rows.length
      ? rows.map((x) => x.ts.slice(11, 19) + '  ' + x.action_id + '  ' + x.event + ': ' + x.detail).join('\n')
      : 'No history yet.';
  } catch (e) {
    $('history').textContent = 'Could not load history.';
  }
}

$('check').onclick = check;
$('refresh').onclick = loadHistory;
$('sample').onclick = () => {
  $('msg').value = 'This is CBI. Do not tell anyone and stay on the video call. Transfer Rs 40000 immediately to prize.claim@fakebank or you will be arrested.';
};
loadHistory();
</script></body></html>
"""


async def index(request):
    return HTMLResponse(PAGE)


async def api_check(request):
    try:
        body = await request.json()
        text = str(body.get("message", "")).strip()[:2000]
        if not text:
            return JSONResponse({"error": "Please paste a message."})
        verdict = await call_tool("check_scam", {"message": text})

        actions = []
        if verdict["risk_level"] in ("medium", "high"):
            reqs = [{"action_type": "notify_child",
                     "target": "Suspicious message: " + text[:80], "message": text}]
            upi = UPI_RE.findall(text)
            link = URL_RE.findall(text)
            if upi:
                reqs.append({"action_type": "block_item", "target": upi[0], "kind": "upi", "message": text})
            elif link:
                reqs.append({"action_type": "block_item", "target": link[0], "kind": "link", "message": text})
            for r in reqs:
                p = await call_tool("preview_action", r)
                if "error" not in p:
                    actions.append(p)
        return JSONResponse({"verdict": verdict, "actions": actions})
    except Exception:
        return JSONResponse({"error": "Something went wrong. Nothing was run."}, status_code=500)


async def api_decide(request):
    try:
        body = await request.json()
        action_id = str(body.get("action_id", ""))
        role = str(body.get("role", ""))
        decision = str(body.get("decision", ""))

        res = await call_tool("decide_action",
                              {"action_id": action_id, "approver": role, "decision": decision})
        if "error" in res:
            return JSONResponse({"error": res["error"]})
        if res["status"] in ("expired", "denied"):
            return JSONResponse({"status": res["status"]})

        ex = await call_tool("execute_action", {"action_id": action_id})
        if "error" in ex:
            return JSONResponse({"error": ex["error"]})
        ver = await call_tool("verify_action", {"action_id": action_id})
        return JSONResponse({"status": "executed", "result": ex["result"],
                             "verified": bool(ver.get("verified"))})
    except Exception:
        return JSONResponse({"error": "Something went wrong. Nothing was run."}, status_code=500)


async def api_history(request):
    try:
        rows = await call_tool("get_history", {"limit": 10})
        return JSONResponse({"rows": rows})
    except Exception:
        return JSONResponse({"rows": []})


app = Starlette(routes=[
    Route("/", index),
    Route("/api/check", api_check, methods=["POST"]),
    Route("/api/decide", api_decide, methods=["POST"]),
    Route("/api/history", api_history),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)