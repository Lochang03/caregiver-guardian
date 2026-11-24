import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN")
url = f"https://api.telegram.org/bot{token}/getMe"

print("proxy env set:", {k: bool(os.environ.get(k)) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")})


def attempt(label, **client_kwargs):
    start = time.time()
    try:
        with httpx.Client(timeout=15, **client_kwargs) as client:
            data = client.get(url).json()
        name = data.get("result", {}).get("username")
        print(label, "OK" if data.get("ok") else "API ERROR", name, f"{time.time() - start:.1f}s")
    except Exception as e:
        print(label, "FAILED", type(e).__name__, f"{time.time() - start:.1f}s")


attempt("default       ")
attempt("IPv4 only     ", transport=httpx.HTTPTransport(local_address="0.0.0.0"))
attempt("ignore proxies", trust_env=False)