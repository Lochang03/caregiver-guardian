import json
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("caregiver-guardian")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

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


def load_blocklist() -> dict:
    try:
        with open(DATA_DIR / "blocklist.json", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"links": [], "upi_ids": [], "phones": []}


def digits(s: str) -> str:
    return re.sub(r"\D", "", s)[-10:]


@mcp.tool()
def ping() -> str:
    """Health check."""
    return "ok"


@mcp.tool()
def check_scam(message: str, sender: str = "") -> dict:
    """Check a message for scam signals. Returns a risk level, a score and the reasons."""
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
    for link in blocklist.get("links", []):
        if link.lower() in lower:
            score += 50
            reasons.append("Link is on the family blocklist")
            evidence.append(link)

    for upi in UPI_RE.findall(lower):
        if upi in [u.lower() for u in blocklist.get("upi_ids", [])]:
            score += 50
            reasons.append("UPI ID is on the family blocklist")
            evidence.append(upi)

    if sender and digits(sender) in [digits(p) for p in blocklist.get("phones", [])]:
        score += 40
        reasons.append("Sender number is on the family blocklist")
        evidence.append(sender)

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 30 else "low"
    return {"risk_level": level, "score": score, "reasons": reasons, "evidence": evidence}


if __name__ == "__main__":
    mcp.run()