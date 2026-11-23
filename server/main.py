import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("caregiver-guardian")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "guardian.db"
APPROVAL_MINUTES = int(os.getenv("APPROVAL_MINUTES", "10"))

ACTIONS = {"notify_child", "block_item", "create_reminder"}
KINDS = {"link", "upi", "phone"}

# (rule name, points, explanation, keywords). Weights are placeholders to tune on test messages.
RULES = [
    ("urgency", 20, "Creates urgency",
     ["urgent", "immediately", "within 24 hours", "last warning", "right now", "turant"]),
    ("authority", 25, "Claims to be an authority (police, bank, government)",
     ["police", "cbi", "customs", "rbi", "court", "arrest", "arrested", "giraftar", "bank officer", "trai"]),
    ("money_or_credentials", 30, "Asks for money or secret codes",
     ["transfer", "send money", "processing fee", "otp", "cvv", "pin", "password", "gift card", "paise bhejo"]),
    ("secrecy", 25, "Tells you to keep it secret or stay on the call",
     ["do not tell", "don't tell", "dont tell", "keep this confidential", "stay on the call", "stay on the video call"]),
]

URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[\w.-]+\.(?:com|in|net|org|xyz|top|link|example)(?:/\S*)?)")
UPI_RE = re.compile(r"\b[\w.\-]{2,}@[a-zA-Z]{2,}\b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY, action_type TEXT, kind TEXT, target TEXT,
    risk_level TEXT, reasons TEXT, approver TEXT, status TEXT,
    created_at TEXT, expires_at TEXT, decided_at TEXT, executed_at TEXT,
    verified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action_id TEXT, event TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS family_blocklist (
    kind TEXT, value TEXT, added_at TEXT, UNIQUE(kind, value)
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, due TEXT, created_at TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def q(sql: str, params: tuple = ()) -> list:
    DATA_DIR.mkdir(exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        cur = conn.execute(sql, params)
        conn.commit()
        return [dict(r) for r in cur.fetchall()]


def log(action_id: str, event: str, detail: str = "") -> None:
    q("INSERT INTO audit (ts, action_id, event, detail) VALUES (?, ?, ?, ?)",
      (stamp(now()), action_id, event, detail))


def load_blocklist() -> dict:
    try:
        with open(DATA_DIR / "blocklist.json", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    out = {
        "links": list(data.get("links", [])),
        "upi_ids": list(data.get("upi_ids", [])),
        "phones": list(data.get("phones", [])),
    }
    key_for = {"link": "links", "upi": "upi_ids", "phone": "phones"}
    for r in q("SELECT kind, value FROM family_blocklist"):
        key = key_for.get(r["kind"])
        if key:
            out[key].append(r["value"])
    return out


def digits(s: str) -> str:
    return re.sub(r"\D", "", s)[-10:]


def _score(message: str, sender: str = "") -> dict:
    text = (message or "").strip()[:2000]
    if not text:
        return {"risk_level": "unknown", "score": 0, "reasons": ["Message is empty"], "evidence": []}

    lower = text.lower().replace("\u2019", "'")
    blocklist = load_blocklist()
    score = 0
    reasons, evidence = [], []

    for _name, points, explanation, keywords in RULES:
        hits = [k for k in keywords if re.search(r"\b" + re.escape(k) + r"\b", lower)]
        if hits:
            score += points
            reasons.append(explanation)
            evidence.extend(hits)

    if URL_RE.search(lower):
        score += 10
        reasons.append("Contains a link")
    for link in blocklist["links"]:
        if link.lower() in lower:
            score += 50
            reasons.append("Link is on the family blocklist")
            evidence.append(link)

    blocked_upi = [u.lower() for u in blocklist["upi_ids"]]
    for upi in UPI_RE.findall(lower):
        if upi in blocked_upi:
            score += 50
            reasons.append("UPI ID is on the family blocklist")
            evidence.append(upi)

    if sender and digits(sender) in [digits(p) for p in blocklist["phones"]]:
        score += 40
        reasons.append("Sender number is on the family blocklist")
        evidence.append(sender)

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 30 else "low"
    return {"risk_level": level, "score": score, "reasons": reasons, "evidence": evidence}


def required_approver(action_type: str, risk_level: str) -> str:
    # Low-impact alerts can be approved by the parent; anything else needs the child.
    if action_type == "notify_child" and risk_level != "high":
        return "parent"
    return "child"


def describe(action_type: str, kind: str, target: str) -> str:
    if action_type == "notify_child":
        return f"Send the child an alert: {target}"
    if action_type == "block_item":
        return f"Add the {kind} '{target}' to the family blocklist"
    return f"Create a reminder: {target}"


def get_action(action_id: str):
    rows = q("SELECT * FROM actions WHERE id = ?", (action_id,))
    return rows[0] if rows else None


@mcp.tool()
def ping() -> str:
    """Health check."""
    return "ok"


@mcp.tool()
def check_scam(message: str, sender: str = "") -> dict:
    """Check a message for scam signals. Returns a risk level, a score and the reasons."""
    return _score(message, sender)


@mcp.tool()
def preview_action(action_type: str, target: str, message: str = "",
                   sender: str = "", kind: str = "link") -> dict:
    """Preview an action BEFORE it runs. Nothing is executed.
    action_type: notify_child, block_item or create_reminder.
    kind (only for block_item): link, upi or phone."""
    action_type = (action_type or "").strip()
    target = (target or "").strip()[:300]
    if action_type not in ACTIONS:
        return {"error": f"Unknown action_type. Use one of: {sorted(ACTIONS)}"}
    if not target:
        return {"error": "target is required"}
    if action_type == "block_item" and kind not in KINDS:
        return {"error": f"kind must be one of: {sorted(KINDS)}"}

    if (message or "").strip():
        risk = _score(message, sender)
    else:
        risk = {"risk_level": "unknown", "score": 0,
                "reasons": ["No message supplied, so no risk check was run"], "evidence": []}

    action_id = uuid.uuid4().hex[:8]
    approver = required_approver(action_type, risk["risk_level"])
    created = now()
    expires = created + timedelta(minutes=APPROVAL_MINUTES)
    q("INSERT INTO actions (id, action_type, kind, target, risk_level, reasons, approver, "
      "status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
      (action_id, action_type, kind, target, risk["risk_level"], json.dumps(risk["reasons"]),
       approver, stamp(created), stamp(expires)))
    log(action_id, "previewed", describe(action_type, kind, target))
    return {
        "action_id": action_id,
        "what_will_happen": describe(action_type, kind, target),
        "risk_level": risk["risk_level"],
        "reasons": risk["reasons"],
        "needs_approval_from": approver,
        "expires_at": stamp(expires),
        "status": "pending",
    }


@mcp.tool()
def decide_action(action_id: str, approver: str, decision: str) -> dict:
    """Approve or deny a pending action. approver: parent or child. decision: approve or deny."""
    row = get_action(action_id)
    if not row:
        return {"error": "Unknown action_id"}
    if approver not in ("parent", "child"):
        return {"error": "approver must be 'parent' or 'child'"}
    if decision not in ("approve", "deny"):
        return {"error": "decision must be 'approve' or 'deny'"}
    if row["status"] != "pending":
        return {"error": f"Action is already '{row['status']}'"}
    if datetime.fromisoformat(row["expires_at"]) < now():
        q("UPDATE actions SET status = 'expired' WHERE id = ?", (action_id,))
        log(action_id, "expired", "Approval window passed; action cancelled, not run")
        return {"action_id": action_id, "status": "expired",
                "note": "Approval window passed. The action was cancelled, not run."}
    if row["approver"] == "child" and approver != "child":
        log(action_id, "approval_refused", f"{approver} tried to decide; child approval required")
        return {"error": "This action needs the child's approval", "needs_approval_from": "child"}

    new_status = "approved" if decision == "approve" else "denied"
    q("UPDATE actions SET status = ?, decided_at = ? WHERE id = ?",
      (new_status, stamp(now()), action_id))
    log(action_id, new_status, f"by {approver}")
    return {"action_id": action_id, "status": new_status}


@mcp.tool()
def execute_action(action_id: str) -> dict:
    """Run an APPROVED action. Refuses anything that is not approved. No real money moves."""
    row = get_action(action_id)
    if not row:
        return {"error": "Unknown action_id"}
    if row["status"] != "approved":
        log(action_id, "execute_refused", f"status was '{row['status']}'")
        return {"error": f"Cannot execute: status is '{row['status']}', not 'approved'"}

    kind = row["action_type"]
    if kind == "notify_child":
        log(action_id, "child_alerted", row["target"])
        result = "Alert recorded for the child (the actual send is simulated)"
    elif kind == "block_item":
        q("INSERT OR IGNORE INTO family_blocklist (kind, value, added_at) VALUES (?, ?, ?)",
          (row["kind"], row["target"], stamp(now())))
        result = f"Added {row['kind']} '{row['target']}' to the family blocklist"
    else:
        q("INSERT INTO reminders (title, due, created_at) VALUES (?, ?, ?)",
          (row["target"], stamp(now() + timedelta(days=1)), stamp(now())))
        result = f"Reminder created: {row['target']}"

    q("UPDATE actions SET status = 'executed', executed_at = ? WHERE id = ?",
      (stamp(now()), action_id))
    log(action_id, "executed", result)
    return {"action_id": action_id, "status": "executed", "result": result}


@mcp.tool()
def verify_action(action_id: str) -> dict:
    """Check that an executed action really took effect."""
    row = get_action(action_id)
    if not row:
        return {"error": "Unknown action_id"}
    if row["status"] not in ("executed", "verified"):
        return {"error": f"Nothing to verify: status is '{row['status']}'"}

    kind = row["action_type"]
    if kind == "notify_child":
        ok = bool(q("SELECT 1 FROM audit WHERE action_id = ? AND event = 'child_alerted'", (action_id,)))
    elif kind == "block_item":
        ok = bool(q("SELECT 1 FROM family_blocklist WHERE kind = ? AND value = ?",
                    (row["kind"], row["target"])))
    else:
        ok = bool(q("SELECT 1 FROM reminders WHERE title = ?", (row["target"],)))

    if ok:
        q("UPDATE actions SET status = 'verified', verified = 1 WHERE id = ?", (action_id,))
        log(action_id, "verified", "Effect confirmed")
    else:
        log(action_id, "verify_failed", "Effect not found")
    return {"action_id": action_id, "verified": ok}


@mcp.tool()
def get_history(limit: int = 20) -> list:
    """Show the most recent audit log entries, newest first."""
    limit = max(1, min(int(limit), 100))
    return q("SELECT ts, action_id, event, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,))


if __name__ == "__main__":
    mcp.run()