#!/usr/bin/env python3
"""
Telegram-бот на GigaChat:
• 🎨 рисует картинки по описанию
• 💬 общается в чатах (личных и групповых)
"""
import os
import re
import time
import uuid
import logging
import threading

import requests
import telebot
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────── Настройки ─────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.environ.get("GIGACHAT_CREDENTIALS")

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://api.giga.chat/v1"
VERIFY_SSL = False

MAX_PROMPT_LENGTH = 1000
IMAGE_PER_HOUR = 20      # лимит картинок на пользователя
TEXT_PER_HOUR = 60       # лимит текстовых ответов
COOLDOWN_SECONDS = 5
MAX_HISTORY = 20         # сколько сообщений диалога помним

SYSTEM_PERSONA = (
    "Ты — дружелюбный собеседник, отвечаешь на русском языке. "
    "Отвечай кратко (1-3 предложения), по делу и вежливо."
)

# Ключевые слова, по которым понимаем, что просят КАРТИНКУ
IMAGE_KEYWORDS = (
    "нарисуй", "нарисовать", "сгенерируй картинк", "сгенерируй изображени",
    "сгенерируй фото", "сделай картинк", "сделай фото", "изобрази",
    "draw ", "paint "
)

if not TELEGRAM_BOT_TOKEN or not GIGACHAT_CREDENTIALS:
    raise RuntimeError("❌ Задайте TELEGRAM_BOT_TOKEN и GIGACHAT_CREDENTIALS!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
BOT_ME = bot.get_me()
logger.info("👤 Бот: @%s", BOT_ME.username)

_lock = threading.Lock()
_rate = {"image": {}, "text": {}}
_history = {}  # chat_id -> список сообщений диалога


# ─────────────────────── GigaChat API ────────────────────────

def get_gigachat_token() -> str:
    resp = requests.post(
        OAUTH_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {GIGACHAT_CREDENTIALS}",
        },
        data={"scope": "GIGACHAT_API_PERS"},
        verify=VERIFY_SSL, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def generate_image(token: str, prompt: str) -> bytes:
    """Генерация картинки (function_call: auto)."""
    resp = requests.post(
        f"{API_URL}/chat/completions",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={
            "model": "GigaChat-3-Ultra",
            "messages": [
                {"role": "system", "content": "Ты — профессиональный художник."},
                {"role": "user", "content": prompt},
            ],
            "function_call": "auto",
        },
        verify=VERIFY_SSL, timeout=300,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"].get("content", "")
    match = re.search(r'<img\s+src="([^"]+)"', content)
    if not match:
        raise RuntimeError("GigaChat не вернул картинку")
    img = requests.get(
        f"{API_URL}/files/{match.group(1)}/content",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/jpg"},
        verify=VERIFY_SSL, timeout=60,
    )
    img.raise_for_status()
    return img.content


def generate_text(token: str, chat_id: int, user_text: str) -> str:
    """Текстовый ответ с учётом контекста диалога."""
    history = _history.setdefault(chat_id, [])
    messages = [{"role": "system", "content": SYSTEM_PERSONA}]
    messages += history[-MAX_HISTORY:]
    messages.append({"role": "user", "content": user_text})

    resp = requests.post(
        f"{API_URL}/chat/completions",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"model": "GigaChat-3-Ultra", "messages": messages,
              "temperature": 0.7, "max_tokens": 800},
        verify=VERIFY_SSL, timeout=60,
    )
    resp.raise_for_status()
    answer = resp.json()["choices"][0]["message"]["content"]

    # сохраняем диалог в память
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    if len(history) > MAX_HISTORY * 2:
        del history[:len(history) - MAX_HISTORY * 2]
    return answer


# ─────────────────────── Вспомогательные ────────────────────────

def check_rate_limit(kind: str, user_id: int):
    limit = IMAGE_PER_HOUR if kind == "image" else TEXT_PER_HOUR
    now = time.time()
    with _lock:
        bucket = _rate[kind].setdefault(user_id, [])
        _rate[kind][user_id] = [ts for ts in bucket if now - ts < 3600]
        if len(_rate[kind][user_id]) >= limit:
            return False, f"⏰ Лимит запросов ({limit}/час). Попробуйте позже."
        if _rate[kind][user_id] and now - _rate[kind][user_id][-1] < COOLDOWN_SECONDS:
            return False, "⏳ Подождите пару секунд."
        _rate[kind][user_id].append(now)
        return True, ""


def is_image_request(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in IMAGE_KEYWORDS)


def send_long_text(chat_id: int, text: str, reply_to=None):
    """Telegram ограничивает сообщение 4096 символами — режем на части."""
    first = True
    while text:
        part, text = text[:4096], text[4096:]
        bot.send_message(chat_id, part,
                         reply_to_message_id=reply_to if first else None)
        first = False


# ─────────────────────── Команды ────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message):
    bot.reply_to(message, (
        "👋 Привет! Я умею:\n"
        "🎨 рисовать — напиши «нарисуй …» или /draw промпт\n"
        "💬 общаться — просто пиши мне как собеседнику\n\n"
        "Команды: /start /help /draw /reset"
    ))


@bot.message_handler(commands=["help"])
def handle_help(message):
    bot.reply_to(message, (
        "🎨 *Картинки:* «нарисуй закат над морем» или /draw закат над морем\n"
        "💬 *Общение:* просто напиши сообщение.\n"
        "В группе я отвечаю, только если меня позвали через @ или "
        "ответили на моё сообщение.\n"
        "/reset — забыть контекст диалога"
    ), parse_mode="Markdown")


@bot.message_handler(commands=["reset"])
def handle_reset(message):
    _history.pop(message.chat.id, None)
    bot.reply_to(message, "🧹 Контекст диалога сброшен.")


@bot.message_handler(commands=["draw"])
def handle_draw_command(message):
    prompt = message.text[len("/draw"):].strip()
    if not prompt:
        bot.reply_to(message, "Использование: /draw что нарисовать")
        return
    process_image(message, prompt)


# ─────────────────────── Обработка сообщений ────────────────────────

@bot.message_handler(content_types=["text"],
                     func=lambda m: not m.text.startswith("/"))
def handle_text(message):
    text = message.text.strip()

    # В группах отвечаем только при обращении к боту
    if message.chat.type in ("group", "supergroup"):
        mention = f"@{BOT_ME.username}".lower()
        is_reply = bool(message.reply_to_message and
                        message.reply_to_message.from_user.id == BOT_ME.id)
        if mention in text.lower():
            # убираем упоминание из текста промпта
            text = re.sub(re.escape(mention), "", text,
                          flags=re.IGNORECASE).strip()
        elif not is_reply:
            return  # в группе говорят не с ботом — молчим

    if not text:
        return

    if is_image_request(text):
        process_image(message, text)
    else:
        process_chat(message, text)


def process_image(message, prompt: str):
    if len(prompt) > MAX_PROMPT_LENGTH:
        bot.reply_to(message, f"❌ Слишком длинный промпт (макс. {MAX_PROMPT_LENGTH}).")
        return
    allowed, reason = check_rate_limit("image", message.from_user.id)
    if not allowed:
        bot.reply_to(message, reason)
        return

    bot.send_chat_action(message.chat.id, "upload_photo")
    status = bot.reply_to(message, "🎨 Рисую… это займёт 20-60 сек.")
    try:
        token = get_gigachat_token()
        image = generate_image(token, prompt)
        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass
        bot.send_photo(
            message.chat.id, image,
            caption=f"🎨 _{prompt[:500]}_",
            reply_to_message_id=message.message_id,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Ошибка генерации картинки: %s", e)
        try:
            bot.edit_message_text(f"❌ Не удалось нарисовать: {str(e)[:200]}",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def process_chat(message, text: str):
    allowed, reason = check_rate_limit("text", message.from_user.id)
    if not allowed:
        bot.reply_to(message, reason)
        return

    bot.send_chat_action(message.chat.id, "typing")
    try:
        token = get_gigachat_token()
        answer = generate_text(token, message.chat.id, text)
        send_long_text(message.chat.id, answer, reply_to=message.message_id)
        logger.info("💬 Ответ в чат %s: %s...", message.chat.id, answer[:60])
    except Exception as e:
        logger.error("Ошибка генерации ответа: %s", e)
        bot.reply_to(message, "❌ Не смог ответить, попробуйте ещё раз.")


if __name__ == "__main__":
    logger.info("🚀 Запуск бота (картинки + общение)")
    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
