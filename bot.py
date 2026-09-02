#!/usr/bin/env python3
"""
Интерактивный Telegram-бот для генерации изображений через GigaChat.

Запуск:
    python bot.py

Переменные окружения:
    TELEGRAM_BOT_TOKEN - токен Telegram-бота
    GIGACHAT_CREDENTIALS - credentials для GigaChat API
    ALLOWED_TELEGRAM_USER_IDS - список разрешённых user_id через запятую
    GENERATION_TIMEOUT_SECONDS - таймаут генерации (опционально, по умолчанию 300)
"""
import os
import sys
import asyncio
import logging
import time
import uuid
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Импортируем функции из существующего скрипта
from scripts.generate_and_send import (
    get_gigachat_token,
    generate_image as sync_generate_image,
    OAUTH_URL,
    API_URL,
    VERIFY_SSL,
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.environ.get("GIGACHAT_CREDENTIALS")
ALLOWED_USER_IDS_STR = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
GENERATION_TIMEOUT = int(os.environ.get("GENERATION_TIMEOUT_SECONDS", "300"))

# Парсим разрешённые user_id
ALLOWED_USER_IDS = set()
if ALLOWED_USER_IDS_STR:
    try:
        ALLOWED_USER_IDS = {int(uid.strip()) for uid in ALLOWED_USER_IDS_STR.split(",") if uid.strip()}
    except ValueError as e:
        logger.error(f"Ошибка парсинга ALLOWED_TELEGRAM_USER_IDS: {e}")

# Максимальная длина caption в Telegram
MAX_CAPTION_LENGTH = 1000


def check_config() -> bool:
    """Проверяет наличие обязательных переменных окружения."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GIGACHAT_CREDENTIALS:
        missing.append("GIGACHAT_CREDENTIALS")
    
    if missing:
        logger.error(f"❌ Отсутствуют обязательные переменные окружения: {', '.join(missing)}")
        return False
    
    if not ALLOWED_USER_IDS:
        logger.warning("⚠️ ALLOWED_TELEGRAM_USER_IDS не задан или пуст. Бот не будет работать.")
        return False
    
    logger.info(f"✅ Конфигурация загружена. Разрешено {len(ALLOWED_USER_IDS)} пользователей.")
    return True


def is_user_allowed(user_id: int) -> bool:
    """Проверяет, есть ли пользователь в белом списке."""
    return user_id in ALLOWED_USER_IDS


async def generate_image_async(token: str, prompt: str) -> bytes:
    """Асинхронная обёртка для синхронной функции generate_image."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, sync_generate_image, token, prompt)


def truncate_caption(text: str, max_length: int = MAX_CAPTION_LENGTH) -> str:
    """Обрезает текст до безопасной длины для caption."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    user_id = update.effective_user.id
    request_id = str(uuid.uuid4())[:8]
    
    logger.info(f"[{request_id}] /start от user_id={user_id}")
    
    if not is_user_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещен.")
        logger.warning(f"[{request_id}] Попытка доступа от неразрешённого user_id={user_id}")
        return
    
    help_text = """
🎨 **Бот для генерации изображений через GigaChat**

**Как использовать:**
Просто отправь текстовое сообщение с описанием того, что нужно нарисовать.

**Примеры:**
• Нарисуй кота-космонавта на Луне
• Акварельный пейзаж зимнего леса
• Киберпанк-улица ночью с неоновыми вывесками

**Команды:**
/start — показать это сообщение
/help — помощь
/status — проверить статус бота
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help."""
    await start_command(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /status."""
    user_id = update.effective_user.id
    request_id = str(uuid.uuid4())[:8]
    
    logger.info(f"[{request_id}] /status от user_id={user_id}")
    
    if not is_user_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещен.")
        return
    
    await update.message.reply_text("✅ Бот работает и готов принимать запросы.")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Основной обработчик текстовых сообщений."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.effective_message.message_id
    prompt = update.message.text.strip()
    request_id = str(uuid.uuid4())[:8]
    
    logger.info(f"[{request_id}] Запрос от user_id={user_id}, chat_id={chat_id}, message_id={message_id}")
    logger.info(f"[{request_id}] Промпт: {prompt[:100]}...")
    
    # Проверка доступа
    if not is_user_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещен.")
        logger.warning(f"[{request_id}] Отказано в доступе user_id={user_id}")
        return
    
    # Проверка, что сообщение не пустое
    if not prompt:
        await update.message.reply_text("💬 Пришли текстовый промпт для генерации изображения.")
        return
    
    # Отправляем статусное сообщение
    status_message = await update.message.reply_text("🎨 Генерирую изображение... Это может занять до 1-2 минут.")
    
    start_time = time.time()
    
    try:
        # Получаем токен GigaChat
        logger.info(f"[{request_id}] Получение токена GigaChat...")
        token = get_gigachat_token()
        
        # Генерируем изображение (асинхронно)
        logger.info(f"[{request_id}] Генерация изображения...")
        image_bytes = await asyncio.wait_for(
            generate_image_async(token, prompt),
            timeout=GENERATION_TIMEOUT
        )
        
        duration = time.time() - start_time
        logger.info(f"[{request_id}] Изображение сгенерировано за {duration:.2f}с, размер: {len(image_bytes)} байт")
        
        # Обновляем статусное сообщение
        await status_message.edit_text("📤 Отправляю изображение...")
        
        # Отправляем изображение
        caption = truncate_caption(prompt)
        
        try:
            await update.message.reply_photo(
                photo=image_bytes,
                caption=caption,
            )
            logger.info(f"[{request_id}] Изображение отправлено как photo")
        except Exception as e:
            logger.warning(f"[{request_id}] Не удалось отправить как photo: {e}")
            # Если не получилось отправить как photo, отправляем как document
            await update.message.reply_document(
                document=image_bytes,
                filename="generated_image.jpg",
                caption=caption,
            )
            logger.info(f"[{request_id}] Изображение отправлено как document")
        
        # Удаляем статусное сообщение
        await status_message.delete()
        
        logger.info(f"[{request_id}] ✅ Запрос успешно обработан за {duration:.2f}с")
        
    except asyncio.TimeoutError:
        duration = time.time() - start_time
        logger.error(f"[{request_id}] ❌ Таймаут генерации после {duration:.2f}с")
        await status_message.edit_text("⏱️ Превышено время ожидания генерации. Попробуй ещё раз.")
        
    except Exception as e:
        duration = time.time() - start_time
        error_type = type(e).__name__
        logger.error(f"[{request_id}] ❌ Ошибка после {duration:.2f}с: {error_type}: {e}")
        
        # Определяем понятное сообщение для пользователя
        if "token" in str(e).lower() or "authorization" in str(e).lower():
            user_message = "🔐 Ошибка доступа к GigaChat. Проверь конфигурацию."
        elif "telegram" in str(e).lower():
            user_message = "📤 Не удалось отправить изображение в Telegram."
        else:
            user_message = "❌ Не удалось сгенерировать изображение. Попробуй позже."
        
        try:
            await status_message.edit_text(user_message)
        except Exception as edit_error:
            logger.error(f"[{request_id}] Не удалось обновить статусное сообщение: {edit_error}")
            await update.message.reply_text(user_message)


async def handle_non_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нетекстовых сообщений (фото, видео, документы и т.д.)."""
    user_id = update.effective_user.id
    request_id = str(uuid.uuid4())[:8]
    
    logger.info(f"[{request_id}] Нетекстовое сообщение от user_id={user_id}")
    
    if not is_user_allowed(user_id):
        return
    
    await update.message.reply_text("💬 Пришли текст промпта обычным сообщением.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок."""
    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)
    
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Произошла внутренняя ошибка. Попробуй позже."
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение об ошибке: {e}")


def main() -> None:
    """Главная функция запуска бота."""
    logger.info("=" * 60)
    logger.info("🚀 Запуск интерактивного Telegram-бота GigaChat")
    logger.info("=" * 60)
    
    # Проверяем конфигурацию
    if not check_config():
        logger.error("❌ Конфигурация некорректна. Завершение работы.")
        sys.exit(1)
    
    # Создаём приложение
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    
    # Регистрируем обработчик текстовых сообщений
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )
    
    # Регистрируем обработчик нетекстовых сообщений
    application.add_handler(
        MessageHandler(~filters.TEXT, handle_non_text_message)
    )
    
    # Регистрируем глобальный обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Запускаем бота
    logger.info("✅ Бот запущен и ожидает сообщения...")
    logger.info(f"👥 Разрешённые пользователи: {ALLOWED_USER_IDS}")
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем.")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
