#!/usr/bin/env python3
"""Берёт следующий промпт из базы, генерирует картинку в GigaChat
и отправляет её в Telegram-чат.

Следует официальной документации:
https://developers.sber.ru/docs/ru/gigachat/guides/images-generation
"""
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
    """Получает OAuth-токен GigaChat (согласно документации)."""
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
    """
    Генерация картинки через chat/completions с function_call.
    
    Согласно документации GigaChat:
    - POST /chat/completions с function_call: "auto"
    - Модель возвращает <img src="uuid"> в content
    - Картинку скачиваем через GET /files/{file_id}/content
    - Формат: JPG
    """
    url = f"{API_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    
    # Системный промпт согласно документации для стилизации изображений
    system_prompt = "Ты — профессиональный художник и иллюстратор. Создавай высококачественные изображения."
    
    payload = {
        "model": "GigaChat-2",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "function_call": "auto"  # Обязательный параметр для активации text2image
    }

    logger.info("📤 Отправляю запрос на генерацию картинки...")
    logger.info(f"💬 Промпт: {prompt[:100]}...")
    
    response = requests.post(
        url, headers=headers, json=payload,
        verify=VERIFY_SSL, timeout=300
    )
    response.raise_for_status()
    
    result = response.json()
    
    # Извлекаем контент ответа
    message = result["choices"][0]["message"]
    content = message.get("content", "")
    logger.info(f"📨 Ответ модели: {content[:150]}...")
    
    # Проверяем finish_reason (согласно документации)
    finish_reason = result["choices"][0].get("finish_reason")
    logger.info(f"🏁 finish_reason: {finish_reason}")
    
    # Ищем <img src="uuid"> согласно документации
    match = re.search(r'<img\s+src="([^"]+)"', content)
    if not match:
        raise RuntimeError(f"В ответе не найден идентификатор картинки: {content}")
    
    file_id = match.group(1)
    logger.info(f"🆔 Идентификатор картинки: {file_id}")
    
    # Скачиваем картинку через GET /files/{file_id}/content
    download_url = f"{API_URL}/files/{file_id}/content"
    logger.info("⬇️ Скачиваю картинку...")
    
    resp = requests.get(
        download_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/jpg"
        },
        verify=VERIFY_SSL,
        timeout=60,
    )
    resp.raise_for_status()
    
    logger.info(f"✅ Картинка скачана: {len(resp.content)} байт")
    return resp.content


def send_to_telegram(image: bytes, caption: str) -> None:
    """
    Отправляет картинку в Telegram с подробной диагностикой.
    
    Telegram API:
    - POST /sendPhoto
    - chat_id: ID чата/канала
    - caption: текст (лимит 1024 символа)
    - photo: файл изображения
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    
    logger.info(f"📤 Отправляю в Telegram chat_id={TELEGRAM_CHAT_ID}")
    
    # Telegram имеет строгий лимит 1024 символа для caption
    MAX_CAPTION_LENGTH = 1024
    if len(caption) > MAX_CAPTION_LENGTH:
        original_length = len(caption)
        caption = caption[:MAX_CAPTION_LENGTH - 3] + "..."
        logger.warning(f"⚠️ Caption обрезан с {original_length} до {len(caption)} символов")
    
    logger.info(f"📝 Caption: {caption[:100]}...")
    
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
        files={"photo": ("generated_image.jpg", image, "image/jpeg")},
        timeout=60,
    )
    
    # Подробная диагностика ошибок
    if resp.status_code != 200:
        logger.error(f"❌ Telegram API вернул статус {resp.status_code}")
        logger.error(f"Ответ Telegram: {resp.text}")
        
        try:
            error_data = resp.json()
            error_code = error_data.get("error_code")
            description = error_data.get("description", "")
            
            logger.error(f"Код ошибки: {error_code}")
            logger.error(f"Описание: {description}")
            
            # Подсказки по типичным ошибкам
            if error_code == 400:
                logger.error("💡 Ошибка 400 - возможные причины:")
                logger.error("   • Неверный chat_id (проверьте секрет TELEGRAM_CHAT_ID)")
                logger.error("   • Бот не добавлен в чат/канал")
                logger.error("   • Caption содержит недопустимые символы")
                logger.error("   • Проблема с форматом изображения")
            elif error_code == 401:
                logger.error("💡 Ошибка 401 - неверный TELEGRAM_BOT_TOKEN")
            elif error_code == 403:
                logger.error("💡 Ошибка 403 - бот заблокирован или не имеет прав")
                logger.error("   • Добавьте бота в канал как администратора")
                logger.error("   • Или в группу/чат как участника")
            elif error_code == 429:
                logger.error("💡 Ошибка 429 - превышен лимит запросов (rate limit)")
        except Exception as e:
            logger.error(f"Не удалось распарсить ответ: {e}")
    
    resp.raise_for_status()
    logger.info("✅ Картинка успешно отправлена в Telegram!")


def take_next_prompt() -> tuple[int, str]:
    """Возвращает очередной промпт и сдвигает указатель (по кругу)."""
    prompts = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    if not prompts:
        raise RuntimeError("База промптов пуста")

    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    index = int(state.get("next_index", 0)) % len(prompts)

    state["next_index"] = (index + 1) % len(prompts)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    
    logger.info(f"📊 Текущий индекс: {index}, следующий будет: {state['next_index']}")
    return index, prompts[index]


def main() -> None:
    logger.info("🚀 Запуск генератора изображений GigaChat → Telegram")
    
    # 1. Берём следующий промпт
    index, prompt = take_next_prompt()
    logger.info(f"📝 Промпт #{index}: {prompt}")

    # 2. Получаем токен
    token = get_gigachat_token()
    
    # 3. Генерируем картинку
    image = generate_image(token, prompt)
    
    # 4. Отправляем в Telegram
    caption = f"{prompt}\n\n(промпт #{index})"
    send_to_telegram(image, caption=caption)
    
    logger.info("🎉 Миссия выполнена успешно!")


if __name__ == "__main__":
    main()
