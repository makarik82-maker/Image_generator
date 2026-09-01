#!/usr/bin/env python3
"""Берёт следующий промпт из базы, генерирует картинку в GigaChat
и отправляет её в Telegram-чат."""
import base64
import json
import os
import uuid
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- настройки из секретов/переменных ---------------------------------
GIGACHAT_CLIENT_ID = os.environ["GIGACHAT_CLIENT_ID"]
GIGACHAT_API_KEY = os.environ["GIGACHAT_API_KEY"]
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_B2B")
GIGACHAT_IMAGE_MODEL = os.getenv("GIGACHAT_IMAGE_MODEL", "preview")

TG_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PROMPTS_FILE = Path(os.getenv("PROMPTS_FILE", "prompts/prompts.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat"


def gigachat_token() -> str:
    """OAuth 2.0 токен для GigaChat API."""
    basic = base64.b64encode(f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_API_KEY}".encode()).decode()
    resp = requests.post(
        OAUTH_URL,
        headers={"Authorization": f"Basic {basic}", "RqUID": str(uuid.uuid4())},
        data={"scope": GIGACHAT_SCOPE},
        verify=False,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def generate_image(token: str, prompt: str) -> bytes:
    """Генерация картинки: модель preview возвращает base64 в поле data."""
    resp = requests.post(
        CHAT_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"model": GIGACHAT_IMAGE_MODEL, "prompt": prompt,
              "width": 1024, "height": 1024},
        verify=False,
        timeout=300,
    )
    resp.raise_for_status()
    payload = resp.json()

    for item in payload.get("data", []):
        if item.get("type") == "image":
            b64 = item["content"].split(",", 1)[-1]  # на случай data:image/...;base64,
            return base64.b64decode(b64)

    raise RuntimeError(f"В ответе GigaChat нет картинки: {payload}")


def send_to_telegram(image: bytes, caption: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
        data={"chat_id": TG_CHAT_ID, "caption": caption[:1024]},
        files={"photo": ("image.png", image, "image/png")},
        timeout=60,
    )
    resp.raise_for_status()


def take_next_prompt() -> tuple[int, str]:
    """Возвращает очередной промпт и сдвигает указатель (по кругу)."""
    prompts = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    if not prompts:
        raise RuntimeError("База промптов пуста")

    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    index = int(state.get("next_index", 0)) % len(prompts)

    state["next_index"] = (index + 1) % len(prompts)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return index, prompts[index]


def main() -> None:
    index, prompt = take_next_prompt()
    print(f"Промпт #{index}: {prompt}")

    token = gigachat_token()
    image = generate_image(token, prompt)
    print(f"Картинка готова: {len(image)} байт")

    send_to_telegram(image, caption=f"{prompt}\n(промпт #{index})")
    print("Отправлено в Telegram.")


if __name__ == "__main__":
    main()