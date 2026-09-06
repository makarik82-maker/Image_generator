#!/usr/bin/env python3
"""
Telegram-бот на Qwen (DashScope / Alibaba Cloud Model Studio):
• 🎨 рисует картинки по описанию — Qwen-Image (нативный асинхронный API)
• 💬 общается в чатах (личных и групповых) — Qwen-Plus (OpenAI-совместимый API)
• 🔁 keep-alive: пингует собственный сервис на Render, чтобы тот не засыпал
"""
import os
import re
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import telebot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────── Настройки ─────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
QWEN_API_KEY = os.environ.get("QWEN_API_KEY")

# Базовый URL DashScope:
#   Китай (Пекин):  https://dashscope.aliyuncs.com
#   Сингапур:       https://dashscope-intl.aliyuncs.com
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com").rstrip("/")

QWEN_TEXT_MODEL = os.environ.get("QWEN_TEXT_MODEL", "qwen-plus")
QWEN_IMAGE_MODEL = os.environ.get("QWEN_IMAGE_MODEL", "qwen-image")
# Поддерживаемые размеры: 1664*928 (16:9, дефолт), 1472*1104, 1328*1328, 1104*1472, 928*1664
QWEN_IMAGE_SIZE = os.environ.get("QWEN_IMAGE_SIZE", "1328*1328")

# Нативный API генерации изображений (только асинхронный режим)
IMAGE_TASK_URL = f"{QWEN_BASE_URL}/api/v1/services/aigc/text2image/image-synthesis"
TASK_STATUS_URL = f"{QWEN_BASE_URL}/api/v1/tasks"
# OpenAI-совместимый API для текстовых ответов
TEXT_API_URL = f"{QWEN_BASE_URL}/compatible-mode/v1"

IMAGE_POLL_INTERVAL = 5    # сек между опросами статуса задачи
IMAGE_TIMEOUT = 300        # макс. время ожидания генерации картинки, сек

MAX_TEXT_LENGTH = 1000     # лимит длины текстового сообщения
MAX_IMAGE_PROMPT_LENGTH = 800  # лимит промпта у Qwen-Image — 800 символов
IMAGE_PER_HOUR = 20        # лимит картинок на пользователя
TEXT_PER_HOUR = 60         # лимит текстовых ответов
COOLDOWN_SECONDS = 5
MAX_HISTORY = 20           # сколько сообщений диалога помним

# ─────────────── Keep-alive для Render (чтобы сервис не засыпал) ───────────────
# Сервис засыпает, если не получает запрос дольше ~50 сек —
# пингуем себя чаще этого интервала.
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "30"))
KEEPALIVE_PATH = os.environ.get("KEEPALIVE_PATH", "/health")
# RENDER_EXTERNAL_URL Render подставляет автоматически для web-сервисов
KEEPALIVE_URL = (
    os.environ.get("KEEPALIVE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")

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

if not TELEGRAM_BOT_TOKEN or not QWEN_API_KEY:
    raise RuntimeError("❌ Задайте TELEGRAM_BOT_TOKEN и QWEN_API_KEY!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
BOT_ME = bot.get_me()
logger.info("👤 Бот: @%s", BOT_ME.username)

_lock = threading.Lock()
_rate = {"image": {}, "text": {}}
_history = {}  # chat_id -> список сообщений диалога


# ─────────────────────── Qwen API ────────────────────────

def _qwen_headers(extra: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {QWEN_API_KEY}",
        "Accept": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def generate_image(prompt: str) -> bytes:
    """Генерация картинки через Qwen-Image.

    Асинхронная схема: создаём задачу → опрашиваем статус по task_id →
    при успехе скачиваем картинку по URL (ссылка живёт 24 часа).
    """
    resp = requests.post(
        IMAGE_TASK_URL,
        headers=_qwen_headers({
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",  # обязателен для HTTP-вызовов
        }),
        json={
            "model": QWEN_IMAGE_MODEL,
            "input": {"prompt": prompt},
            "parameters": {
                "size": QWEN_IMAGE_SIZE,
                "n": 1,
                "prompt_extend": True,   # модель сама дополняет промпт
                "watermark": False,
            },
        },
        timeout=60,
    )
    resp.raise_for_status()
    task_id = resp.json()["output"]["task_id"]
    logger.info("🖼 Задача генерации создана: %s", task_id)

    deadline = time.time() + IMAGE_TIMEOUT
    while time.time() < deadline:
        status = requests.get(
            f"{TASK_STATUS_URL}/{task_id}",
            headers=_qwen_headers(), timeout=30,
        )
        status.raise_for_status()
        output = status.json().get("output", {})
        task_status = output.get("task_status")

        if task_status == "SUCCEEDED":
            img_url = output["results"][0]["url"]
            img = requests.get(img_url, timeout=120)
            img.raise_for_status()
            logger.info("🖼 Картинка скачана: %d байт", len(img.content))
            return img.content
        if task_status in ("FAILED", "CANCELED", "UNKNOWN"):
            raise RuntimeError(
                f"Qwen не смог нарисовать: {output.get('code', '')} "
                f"{output.get('message', task_status)}"
            )
        # PENDING / RUNNING — ждём дальше
        time.sleep(IMAGE_POLL_INTERVAL)

    raise TimeoutError("Генерация картинки заняла слишком много времени")


def generate_text(chat_id: int, user_text: str) -> str:
    """Текстовый ответ Qwen с учётом контекста диалога (OpenAI-совместимый API)."""
    history = _history.setdefault(chat_id, [])
    messages = [{"role": "system", "content": SYSTEM_PERSONA}]
    messages += history[-MAX_HISTORY:]
    messages.append({"role": "user", "content": user_text})

    resp = requests.post(
        f"{TEXT_API_URL}/chat/completions",
        headers=_qwen_headers({"Content-Type": "application/json"}),
        json={
            "model": QWEN_TEXT_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 800,
        },
        timeout=60,
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
    if len(prompt) > MAX_IMAGE_PROMPT_LENGTH:
        bot.reply_to(message,
                     f"❌ Слишком длинный промпт (макс. {MAX_IMAGE_PROMPT_LENGTH}).")
        return
    allowed, reason = check_rate_limit("image", message.from_user.id)
    if not allowed:
        bot.reply_to(message, reason)
        return

    bot.send_chat_action(message.chat.id, "upload_photo")
    status = bot.reply_to(message, "🎨 Рисую… это займёт 20-60 сек.")
    try:
        image = generate_image(prompt)
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
        answer = generate_text(message.chat.id, text)
        send_long_text(message.chat.id, answer, reply_to=message.message_id)
        logger.info("💬 Ответ в чат %s: %s...", message.chat.id, answer[:60])
    except Exception as e:
        logger.error("Ошибка генерации ответа: %s", e)
        bot.reply_to(message, "❌ Не смог ответить, попробуйте ещё раз.")


# ─────────────────────── Keep-alive / Render ────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    """Минимальный HTTP-сервер: отвечает 200 OK на любой GET.

    Нужен, чтобы сервис имел веб-эндпоинт на Render (тип "Web Service") —
    на него приходят пинги и health-check.
    """

    def do_GET(self):
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # не спамить логами на каждый пинг
        pass


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    logger.info("🩺 Health-сервер запущен на порту %s", port)


def keepalive_loop():
    """Фоновый поток: каждые KEEPALIVE_INTERVAL секунд отправляет тестовый
    GET на собственный сервис, чтобы Render не укладывал его спать."""
    url = f"{KEEPALIVE_URL}{KEEPALIVE_PATH}"
    logger.info("🔁 Keep-alive: пинг %s каждые %d сек", url, KEEPALIVE_INTERVAL)
    while True:
        try:
            r = requests.get(url, timeout=15)
            logger.info("🔁 keep-alive → HTTP %s", r.status_code)
        except Exception as e:
            logger.warning("🔁 keep-alive ошибка: %s", e)
        time.sleep(KEEPALIVE_INTERVAL)


# ─────────────────────── Запуск ────────────────────────

if __name__ == "__main__":
    logger.info("🚀 Запуск бота (Qwen: картинки + общение)")

    try:
        start_health_server()
    except OSError as e:
        logger.error("Не удалось поднять health-сервер: %s", e)

    if KEEPALIVE_URL:
        threading.Thread(target=keepalive_loop, daemon=True).start()
    else:
        logger.warning("⚠️ KEEPALIVE_URL/RENDER_EXTERNAL_URL не задан — "
                       "пинги Render выполняться не будут")

    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
