import logging
import os
import re
from telegram.request import HTTPXRequest

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from mcp_client import call_tool

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the token out of logs

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHILD_ID = os.getenv("CHILD_CHAT_ID")  # optional. If empty, the bot runs in single-chat demo mode.

URL_RE = re.compile(r"(https?://\S+|www\.\S+)")
UPI_RE = re.compile(r"\b[\w.\-]{2,}@[a-zA-Z]{2,}\b")

LEVEL_TEXT = {
    "high": "HIGH RISK. Do not pay or share anything.",
    "medium": "Be careful. This looks suspicious.",
    "low": "Looks low risk.",
    "unknown": "I could not check this message.",
}


def card_text(p: dict) -> str:
    return (
        f"Proposed action: {p['what_will_happen']}\n"
        f"Risk: {p['risk_level']}\n"
        f"Why: {', '.join(p['reasons'])}\n"
        f"Needs approval from: {p['needs_approval_from']}\n"
        f"(Expires automatically. If nobody answers, it is cancelled, not run.)"
    )


def buttons(action_id: str) -> InlineKeyboardMarkup:
    if CHILD_ID:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve", callback_data=f"approve:auto:{action_id}"),
            InlineKeyboardButton("Deny", callback_data=f"deny:auto:{action_id}"),
        ]])
    # Demo mode: one chat, so the person pressing chooses a role.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Approve as parent", callback_data=f"approve:parent:{action_id}"),
         InlineKeyboardButton("Approve as child", callback_data=f"approve:child:{action_id}")],
        [InlineKeyboardButton("Deny", callback_data=f"deny:child:{action_id}")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Caregiver Guardian (prototype, synthetic data).\n"
        "Forward or paste a suspicious message and I will check it.\n"
        "/history shows the audit log. /myid shows your Telegram ID."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram ID is {update.effective_user.id}")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        rows = await call_tool("get_history", {"limit": 5})
    except Exception:
        logging.exception("get_history failed")
        await update.message.reply_text("Sorry, I could not read the history.")
        return
    if not rows:
        await update.message.reply_text("No history yet.")
        return
    lines = [f"{r['ts'][11:19]} {r['action_id']} {r['event']}: {r['detail']}" for r in rows]
    await update.message.reply_text("\n".join(lines))


async def propose(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = int(CHILD_ID) if CHILD_ID else update.effective_chat.id
    requests = [{"action_type": "notify_child", "target": "Suspicious message: " + text[:80], "message": text}]
    upi = UPI_RE.findall(text)
    link = URL_RE.findall(text)
    if upi:
        requests.append({"action_type": "block_item", "target": upi[0], "kind": "upi", "message": text})
    elif link:
        requests.append({"action_type": "block_item", "target": link[0], "kind": "link", "message": text})

    for req in requests:
        p = await call_tool("preview_action", req)
        if "error" in p:
            logging.warning("preview failed: %s", p["error"])
            continue
        await context.bot.send_message(chat_id, card_text(p), reply_markup=buttons(p["action_id"]))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    try:
        res = await call_tool("check_scam", {"message": text})
    except Exception:
        logging.exception("check_scam failed")
        await update.message.reply_text("Sorry, the checker is not available right now.")
        return

    lines = [LEVEL_TEXT.get(res["risk_level"], res["risk_level"]), f"Score: {res['score']}/100"]
    if res["reasons"]:
        lines.append("Why:\n- " + "\n- ".join(res["reasons"]))
    await update.message.reply_text("\n".join(lines))

    if res["risk_level"] in ("medium", "high"):
        try:
            await propose(update, context, text)
        except Exception:
            logging.exception("propose failed")
            await update.message.reply_text("I could not prepare the action. Nothing was run.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        decision, role, action_id = query.data.split(":")
        if role == "auto":
            role = "child" if str(query.from_user.id) == str(CHILD_ID) else "parent"

        res = await call_tool("decide_action",
                              {"action_id": action_id, "approver": role, "decision": decision})
        if "error" in res:
            await query.message.reply_text(f"Not allowed: {res['error']}")
            return
        if res["status"] == "expired":
            await query.edit_message_text("Expired. The action was cancelled, not run.")
            return
        if res["status"] == "denied":
            await query.edit_message_text("Denied. Nothing was run.")
            return

        ex = await call_tool("execute_action", {"action_id": action_id})
        if "error" in ex:
            await query.edit_message_text(f"Could not run: {ex['error']}")
            return
        ver = await call_tool("verify_action", {"action_id": action_id})
        mark = "verified" if ver.get("verified") else "NOT verified"
        await query.edit_message_text(f"Done: {ex['result']}\nStatus: {mark}")
    except Exception:
        logging.exception("button handling failed")
        await query.message.reply_text("Something went wrong. Nothing was run.")


def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Add it to .env")

    def make_request() -> HTTPXRequest:
        return HTTPXRequest(
            read_timeout=30, write_timeout=30, connect_timeout=30, pool_timeout=30,
            httpx_kwargs={"trust_env": False},  # ignore system proxy settings
        )

    app = (
        Application.builder()
        .token(TOKEN)
        .request(make_request())
        .get_updates_request(make_request())
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=5)
if __name__ == "__main__":
    main()