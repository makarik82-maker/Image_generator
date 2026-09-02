#!/usr/bin/env python3
"""
Telegram-бот для генерации картинок через GigaChat.
Принимает любое сообщение от пользователя → отправляет в GigaChat → отвечает картинкой.

Запуск:
  export GIGACHAT_CREDENTIALS="ваш_ключ"
  export TELEGRAM_BOT_TOKEN="токен_от_BotFather"
  python bot/telegram_bot.py
"""
import os
import re
import uuid
import logging
import threading
from typing import Optional

import requests
import telebot
import urllib3

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# Отключаем предупреждения о SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────── Настройки ─────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.environ.get("GIGACHAT_CREDENTIALS")

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://api.giga.chat/v1"
VERIFY_SSL = False

# Лимиты и защита от спама
MAX_PROMPT_LENGTH = 1000
MAX_REQUESTS_PER_USER_PER_HOUR = 20
REQUEST_COOLDOWN_SECONDS = 10

# Кэш для rate-limiting (user_id -> список timestamps)
user_requests = {}
user_requests_lock = threading.Lock()


# ──────────────────── Проверка окружения ───────────────────────────

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("❌ TELEGRAM_BOT_TOKEN не задан в переменных окружения!")
if not GIGACHAT_CREDENTIALS:
    raise RuntimeError("❌ GIGACHAT_CREDENTIALS не задан в переменных окружения!")


# ──────────────────── Инициализация бота ───────────────────────────

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)


# ──────────────────── Вспомогательные функции ──────────────────────

def check_rate_limit(user_id: int) -> tuple[bool, str]:
    """Проверяет, не превысил ли пользователь лимит запросов."""
    import time
    now = time.time()
    
    with user_requests_lock:
        if user_id not in user_requests:
            user_requests[user_id] = []
        
        # Удаляем старые записи (старше 1 часа)
        user_requests[user_id] = [
            ts for ts in user_requests[user_id]
            if now - ts < 3600
        ]
        
        if len(user_requests[user_id]) >= MAX_REQUESTS_PER_USER_PER_HOUR:
            return False, f"⏰ Превышен лимит: {MAX_REQUESTS_PER_USER_PER_HOUR} запросов в час. Попробуйте позже."
        
        # Проверяем кулдаун между запросами
        if user_requests[user_id] and now - user_requests[user_id][-1] < REQUEST_COOLDOWN_SECONDS:
            wait = int(REQUEST_COOLDOWN_SECONDS - (now - user_requests[user_id][-1]))
            return False, f"⏳ Подождите {wait} сек перед следующим запросом."
        
        user_requests[user_id].append(now)
        return True, ""


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
    return response.json()["access_token"]


def generate_image(token: str, prompt: str) -> bytes:
    """
    Генерирует картинку через GigaChat API.
    Согласно документации: POST /chat/completions с function_call: "auto"
    """
    url = f"{API_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    
    payload = {
        "model": "GigaChat-2",
        "messages": [
            {"role": "system", "content": "Ты — профессиональный художник и иллюстратор."},
            {"role": "user", "content": prompt}
        ],
        "function_call": "auto"
    }

    response = requests.post(
        url, headers=headers, json=payload,
        verify=VERIFY_SSL, timeout=300
    )
    response.raise_for_status()
    
    result = response.json()
    content = result["choices"][0]["message"].get("content", "")
    
    # Извлекаем идентификатор картинки из <img src="uuid">
    match = re.search(r'<img\s+src="([^"]+)"', content)
    if not match:
        raise RuntimeError("GigaChat не вернул картинку для этого промпта")
    
    file_id = match.group(1)
    
    # Скачиваем картинку
    resp = requests.get(
        f"{API_URL}/files/{file_id}/content",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/jpg"
        },
        verify=VERIFY_SSL,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


# ──────────────────── Обработчики команд ────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message: telebot.types.Message):
    """Приветственное сообщение."""
    bot.reply_to(
        message,
        "👋 Привет! Я бот, который рисует картинки по твоему описанию.\n\n"
        "📝 Просто отправь мне текст — и я создам изображение!\n\n"
        "Примеры:\n"
        "• «Кот-космонавт на Луне»\n"
        "• «Акварельный пейзаж зимнего леса»\n"
        "• «Киберпанк-улица ночью»\n\n"
        "⚙️ Команды:\n"
        "/start — это сообщение\n"
        "/help — помощь и примеры\n"
        "/stats — статистика"
    )


@bot.message_handler(commands=["help"])
def handle_help(message: telebot.types.Message):
    """Помощь по использованию бота."""
    bot.reply_to(
        message,
        "🎨 *Как пользоваться ботом*\n\n"
        "Просто отправь мне любое текстовое описание, и я сгенерирую картинку.\n\n"
        "💡 *Советы для лучших результатов:*\n"
        "• Описывай детально\n"
        "• Указывай стиль (акварель, масло, 3D, аниме)\n"
        "• Добавляй атмосферу (мрачная, весёлая, загадочная)\n\n"
        "📊 *Лимиты:*\n"
        f"• {MAX_REQUESTS_PER_USER_PER_HOUR} запросов в час\n"
        f"• {REQUEST_COOLDOWN_SECONDS} сек между запросами\n"
        f"• До {MAX_PROMPT_LENGTH} символов в промпте",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["stats"])
def handle_stats(message: telebot.types.Message):
    """Показывает статистику пользователя."""
    import time
    user_id = message.from_user.id
    now = time.time()
    
    with user_requests_lock:
        recent = user_requests.get(user_id, [])
        recent = [ts for ts in recent if now - ts < 3600]
    
    bot.reply_to(
        message,
        f"📊 *Твоя статистика за последний час:*\n"
        f"• Запросов: {len(recent)}/{MAX_REQUESTS_PER_USER_PER_HOUR}\n"
        f"• Доступно: {MAX_REQUESTS_PER_USER_PER_HOUR - len(recent)}",
        parse_mode="Markdown"
    )


# ──────────────── Основной обработчик сообщений ───────────────────

@bot.message_handler(content_types=["text"], func=lambda m: not m.text.startswith("/"))
def handle_text_message(message: telebot.types.Message):
    """Главный обработчик: получает промпт и отвечает картинкой."""
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    prompt = message.text.strip()
    
    logger.info(f"📨 Запрос от @{username} (ID {user_id}): {prompt[:80]}...")
    
    # 1. Проверяем длину
    if len(prompt) > MAX_PROMPT_LENGTH:
        bot.reply_to(
            message,
            f"❌ Промпт слишком длинный ({len(prompt)} символов). "
            f"Максимум {MAX_PROMPT_LENGTH}."
        )
        return
    
    if len(prompt) < 3:
        bot.reply_to(message, "❌ Слишком короткое описание. Напиши подробнее!")
        return
    
    # 2. Проверяем rate-limit
    allowed, reason = check_rate_limit(user_id)
    if not allowed:
        bot.reply_to(message, reason)
        return
    
    # 3. Отправляем "typing" статус
    bot.send_chat_action(message.chat.id, "upload_photo")
    
    # 4. Отправляем уведомление о начале работы
    status_msg = bot.reply_to(message, "🎨 Генерирую картинку... это займёт 20-60 сек.")
    
    # 5. Генерируем картинку
    try:
        token = get_gigachat_token()
        image = generate_image(token, prompt)
        
        logger.info(f"✅ Картинка готова: {len(image)} байт")
        
        # 6. Удаляем статус-сообщение
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass
        
        # 7. Отправляем картинку как ответ на исходное сообщение
        caption = f"🎨 По запросу: _{prompt[:500]}_"
        bot.send_photo(
            chat_id=message.chat.id,
            photo=image,
            caption=caption,
            reply_to_message_id=message.message_id,
            parse_mode="Markdown"
        )
        
        logger.info(f"✅ Отправлено в чат {message.chat.id}")
        
    except requests.exceptions.Timeout:
        bot.edit_message_text(
            "❌ GigaChat слишком долго думал. Попробуй ещё раз.",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )
        logger.error("⏱️ Timeout от GigaChat API")
        
    except requests.exceptions.HTTPError as e:
        error_text = f"❌ Ошибка GigaChat API: {e.response.status_code}"
        try:
            bot.edit_message_text(
                error_text,
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
        except Exception:
            bot.reply_to(message, error_text)
        logger.error(f"GigaChat HTTP error: {e}")
        
    except Exception as e:
        error_text = f"❌ Не удалось сгенерировать: {str(e)[:200]}"
        try:
            bot.edit_message_text(
                error_text,
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
        except Exception:
            bot.reply_to(message, error_text)
        logger.error(f"Ошибка генерации: {e}", exc_info=True)


# ──────────────────── Запуск бота ──────────────────────────────────

if __name__ == "__main__":
    logger.info("🚀 Запуск GigaChat Image Bot")
    logger.info(f"👤 Бот: @{bot.get_me().username}")
    
    # Включаем long polling
    # remove_webhook нужен на случай, если был настроен webhook ранее
    bot.remove_webhook()
    
    # infinity_polling автоматически переподключается при обрывах
    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60,
        skip_pending=True  # пропускаем накопившиеся сообщения
      )
