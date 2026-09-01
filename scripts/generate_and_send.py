#!/usr/bin/env python3
"""Берёт следующий промпт из базы, генерирует картинку в GigaChat
и отправляет её в Telegram-чат."""
import json
import os
import re
import uuid
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- настройки из секретов/переменных ---------------------------------
GIGACHAT_AUTH_KEY = os.environ["GIGACHAT_AUTH_KEY"]
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

TG_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PROMPTS_FILE = Path(os.getenv("PROMPTS_FILE", "prompts/prompts.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_BASE_URL = "https://api.giga.chat/api/v1"


def gigachat_token() -> str:
    """OAuth 2.0 токен для GigaChat API."""
    resp = requests.post(
        OAUTH_URL,
        headers={
            "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data=f"scope={GIGACHAT_SCOPE}",
        verify=False,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def list_models(token: str) -> list:
    """Получить список доступных моделей."""
    resp = requests.get(
        f"{API_BASE_URL}/models",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        verify=False,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def generate_image(token: str, prompt: str, model: str) -> bytes:
    """Генерация картинки через chat/completions с function_call."""
    print(f"Используем модель: {model}")
    
    # Запрос на генерацию
    resp = requests.post(
        f"{API_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "function_call": "auto"
        },
        verify=False,
        timeout=300,
    )
    
    if resp.status_code == 403:
        print(f"Ответ сервера: {resp.text}")
        raise RuntimeError(f"Доступ запрещён. Ответ: {resp.text}")
    
    resp.raise_for_status()
    payload = resp.json()
    
    # Извлекаем идентификатор картинки из ответа
    content = payload["choices"][0]["message"]["content"]
    print(f"Ответ модели: {content}")
    
    # Ищем <img src="uuid">
    match = re.search(r'<img\s+src="([^"]+)"', content)
    if not match:
        raise RuntimeError(f"В ответе не найден идентификатор картинки: {content}")
    
    file_id = match.group(1)
    print(f"Идентификатор картинки: {file_id}")
    
    # Скачиваем картинку
    resp = requests.get(
        f"{API_BASE_URL}/files/{file_id}/content",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/jpg",
        },
        verify=False,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def send_to_telegram(image: bytes, caption: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
        data={"chat_id": TG_CHAT_ID, "caption": caption[:1024]},
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
    print(f"Промпт #{index}: {prompt}")

    token = gigachat_token()
    print("Токен получен успешно")
    
    # Получаем список моделей
    models = list_models(token)
    print(f"Доступные модели: {[m['id'] for m in models]}")
    
    # Пробуем модели по порядку
    for model in models:
        model_id = model['id']
        try:
            print(f"\nПытаемся использовать модель: {model_id}")
            image = generate_image(token, prompt, model_id)
            print(f"Картинка готова: {len(image)} байт")
            send_to_telegram(image, caption=f"{prompt}\n(промпт #{index}, модель {model_id})")
            print("Отправлено в Telegram.")
            return
        except Exception as e:
            print(f"Ошибка с моделью {model_id}: {e}")
            continue
    
    raise RuntimeError("Не удалось сгенерировать картинку ни с одной моделью")


if __name__ == "__main__":
    main()
