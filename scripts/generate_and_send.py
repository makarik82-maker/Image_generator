#!/usr/bin/env python3
"""Берёт следующий промпт из базы, генерирует картинку в GigaChat
и отправляет её в Telegram-чат."""
import json
import os
import re
import uuid
import logging
from pathlib import Path

import requests
import urllib3

# Подавляем предупреждения о непроверенных HTTPS-запросах
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- настройки из секретов/переменных ---------------------------------
GIGACHAT_CREDENTIALS = os.environ["GIGACHAT_CREDENTIALS"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PROMPTS_FILE = Path(os.getenv("PROMPTS_FILE", "prompts/prompts.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://api.giga.chat/v1"
VERIFY_SSL = False


def get_gigachat_token() -> str:
    """Получает OAuth-токен GigaChat (как в рабочем коде)."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {GIGACHAT_CREDENTIALS}",
    }
    data = {"scope": "GIGACHAT_API_PERS"}

    response = requests.post(
        OAUTH_URL, headers=headers, data=data,
        verify=VERIFY_SSL, timeout=30
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    logger.info("✅ Токен GigaChat успешно получен")
    return token


def generate_image(token: str, prompt: str) -> bytes:
    """Генерация картинки через chat/completions с function_call."""
    url = f"{API_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    
    payload = {
        "model": "GigaChat-2",
        "messages": [
            {"role": "system", "content": "Ты — художник. Нарисуй то, что тебя попросят."},
            {"role": "user", "content": prompt}
        ],
        "function_call": "auto"
    }

    logger.info("📤 Отправляю запрос на генерацию картинки...")
    response = requests.post(
        url, headers=headers, json=payload,
        verify=VERIFY_SSL, timeout=300
    )
    response.raise_for_status()
    
    result = response.json()
    content = result["choices"][0]["message"]["content"]
    logger.info(f"Ответ модели: {content[:100]}...")
    
    # Ищем <img src="uuid">
    match = re.search(r'<img\s+src="([^"]+)"', content)
    if not match:
        raise RuntimeError(f"В ответе не найден идентификатор картинки: {content}")
    
    file_id = match.group(1)
    logger.info(f"🆔 Идентификатор картинки: {file_id}")
    
    # Скачиваем картинку
    download_url = f"{API_URL}/files/{file_id}/content"
    logger.info("⬇️ Скачиваю картинку...")
    resp = requests.get(
        download_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/jpg"},
        verify=VERIFY_SSL,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def send_to_telegram(image: bytes, caption: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
        files={"photo": ("image.jpg", image, "image/jpeg")},
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
    logger.info(f"📝 Промпт #{index}: {prompt}")

    token = get_gigachat_token()
    image = generate_image(token, prompt)
    logger.info(f"🖼️ Картинка готова: {len(image)} байт")

    send_to_telegram(image, caption=f"{prompt}\n(промпт #{index})")
    logger.info("✅ Отправлено в Telegram.")


if __name__ == "__main__":
    main()
