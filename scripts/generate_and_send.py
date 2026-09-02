#!/usr/bin/env python3
"""Берёт ВСЕ промпты из базы, генерирует картинки в GigaChat
и отправляет их в Telegram-чат по очереди.

Следует официальной документации:
https://developers.sber.ru/docs/ru/gigachat/guides/images-generation
"""
import json
import os
import re
import uuid
import logging
import time
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

# Задержка между отправками в Telegram (секунды)
TELEGRAM_DELAY = 2


def get_gigachat_token() -> str:
    """Получает OAuth-токен GigaChat."""
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
    """
    url = f"{API_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    
    system_prompt = "Ты — профессиональный художник и иллюстратор. Создавай высококачественные изображения."
    
    payload = {
        "model": "GigaChat-2",
        "messages": [
            {"role": "system", "content": system_prompt},
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
    message = result["choices"][0]["message"]
    content = message.get("content", "")
    
    finish_reason = result["choices"][0].get("finish_reason")
    logger.info(f"🏁 finish_reason: {finish_reason}")
    
    match = re.search(r'<img\s+src="([^"]+)"', content)
    if not match:
        raise RuntimeError(f"В ответе не найден идентификатор картинки: {content}")
    
    file_id = match.group(1)
    logger.info(f"🆔 Идентификатор картинки: {file_id}")
    
    download_url = f"{API_URL}/files/{file_id}/content"
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
    """Отправляет картинку в Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    
    MAX_CAPTION_LENGTH = 1024
    if len(caption) > MAX_CAPTION_LENGTH:
        caption = caption[:MAX_CAPTION_LENGTH - 3] + "..."
        logger.warning(f"⚠️ Caption обрезан до {len(caption)} символов")
    
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
        files={"photo": ("generated_image.jpg", image, "image/jpeg")},
        timeout=60,
    )
    
    if resp.status_code != 200:
        logger.error(f"❌ Telegram API вернул статус {resp.status_code}")
        logger.error(f"Ответ Telegram: {resp.text}")
        
        try:
            error_data = resp.json()
            error_code = error_data.get("error_code")
            description = error_data.get("description", "")
            
            logger.error(f"Код ошибки: {error_code}, Описание: {description}")
            
            if error_code == 400:
                logger.error("💡 Возможные причины: неверный chat_id, бот не в чате, caption слишком длинный")
            elif error_code == 401:
                logger.error("💡 Неверный TELEGRAM_BOT_TOKEN")
            elif error_code == 403:
                logger.error("💡 Бот заблокирован или не имеет прав")
            elif error_code == 429:
                logger.error("💡 Превышен лимит запросов")
        except Exception as e:
            logger.error(f"Не удалось распарсить ответ: {e}")
    
    resp.raise_for_status()
    logger.info("✅ Картинка успешно отправлена в Telegram!")


def main() -> None:
    logger.info("🚀 Запуск генератора ВСЕХ изображений GigaChat → Telegram")
    
    # Загружаем все промпты
    prompts = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    if not prompts:
        raise RuntimeError("База промптов пуста")
    
    logger.info(f"📚 Загружено промптов: {len(prompts)}")
    
    # Получаем токен один раз на все промпты
    token = get_gigachat_token()
    
    # Проходим по ВСЕМ промптам
    for index, prompt in enumerate(prompts, start=1):
        logger.info(f"\n{'='*60}")
        logger.info(f"📝 Промпт {index}/{len(prompts)}: {prompt}")
        logger.info(f"{'='*60}")
        
        try:
            # Генерируем картинку
            image = generate_image(token, prompt)
            
            # Отправляем в Telegram
            caption = f"{prompt}\n\n(промпт {index}/{len(prompts)})"
            send_to_telegram(image, caption=caption)
            
            logger.info(f"✅ Промпт {index}/{len(prompts)} успешно обработан!")
            
            # Задержка между отправками
            if index < len(prompts):
                logger.info(f"⏳ Пауза {TELEGRAM_DELAY} сек перед следующим промптом...")
                time.sleep(TELEGRAM_DELAY)
                
        except Exception as e:
            logger.error(f"❌ Ошибка при обработке промпта {index}: {e}")
            logger.error("Продолжаю с следующим промптом...")
            continue
    
    logger.info(f"\n🎉 Обработка всех {len(prompts)} промптов завершена!")


if __name__ == "__main__":
    main()
