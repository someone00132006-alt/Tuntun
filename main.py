import os
import logging
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-secret")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
BASE_URL = os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT", "You are a helpful, friendly assistant. Reply in the user's language."
)

# In-memory state (resets on restart)
history = defaultdict(lambda: deque(maxlen=20))  # last 20 messages per chat
users = set()
banned = set()
client: httpx.AsyncClient = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=60)
    if BASE_URL:
        r = await client.post(
            f"{TG}/setWebhook",
            json={
                "url": f"{BASE_URL}/webhook",
                "secret_token": WEBHOOK_SECRET,
                "drop_pending_updates": True,
                "allowed_updates": ["message"],
            },
        )
        log.info("setWebhook: %s", r.text)
    yield
    await client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok"}


async def send(chat_id: int, text: str):
    for i in range(0, len(text), 4000):
        await client.post(f"{TG}/sendMessage", json={"chat_id": chat_id, "text": text[i:i + 4000]})


async def ask_gemini(chat_id: int, text: str) -> str:
    history[chat_id].append({"role": "user", "parts": [{"text": text}]})
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": list(history[chat_id]),
    }
    try:
        r = await client.post(GEMINI_URL, json=payload, headers={"x-goog-api-key": GEMINI_API_KEY})
        r.raise_for_status()
        reply = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.error("Gemini error: %s", e)
        history[chat_id].pop()
        return "Sorry, abhi error aa gaya. Thodi der baad try karo."
    history[chat_id].append({"role": "model", "parts": [{"text": reply}]})
    return reply


async def handle(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    if not text or uid in banned:
        return
    users.add(chat_id)
    is_admin = uid == ADMIN_ID

    if text.startswith("/start"):
        return await send(chat_id, "Hi! Mujhse kuch bhi poochho. /reset se chat clear hoti hai.")
    if text.startswith("/reset"):
        history.pop(chat_id, None)
        return await send(chat_id, "Chat history clear ho gayi.")

    # Admin commands
    if is_admin and text.startswith("/stats"):
        return await send(chat_id, f"Users: {len(users)}\nBanned: {len(banned)}")
    if is_admin and text.startswith("/ban "):
        banned.add(int(text.split()[1]))
        return await send(chat_id, "Banned.")
    if is_admin and text.startswith("/unban "):
        banned.discard(int(text.split()[1]))
        return await send(chat_id, "Unbanned.")
    if is_admin and text.startswith("/broadcast "):
        body = text.split(" ", 1)[1]
        ok = 0
        for u in list(users):
            try:
                await send(u, body)
                ok += 1
            except Exception:
                pass
        return await send(chat_id, f"Sent to {ok}/{len(users)}")

    await client.post(f"{TG}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
    await send(chat_id, await ask_gemini(chat_id, text))


@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    update = await request.json()
    msg = update.get("message")
    if msg:
        bg.add_task(handle, msg)  # reply 200 immediately, process in background
    return {"ok": True}
