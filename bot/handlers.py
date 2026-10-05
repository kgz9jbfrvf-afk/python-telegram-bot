"""Telegram update handlers."""

import logging

import asyncio 
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from telegram import ReplyKeyboardMarkup, Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import cache, db


logger = logging.getLogger(__name__)

# Keys used to read shared connections from Application.bot_data.
DB_KEY = "db"
REDIS_KEY = "redis"

# In-memory fallback for the per-user message counter when Redis is unavailable.
# Process-local and non-persistent, but keeps the feature working for local testing.
_LOCAL_MESSAGE_COUNTS: dict[int, int] = {}

BOT_COMMANDS = (
    ("start", "Show the main menu"),
    ("help", "Show help"),
    ("about", "Show bot information"),
    ("ping", "Check bot status"),
    ("news", "Latest Minecraft news"),
)

MENU_HELP = "Help"
MENU_ABOUT = "About"
MENU_PING = "Ping" 

MENU_NEWS = "📰 Новости"
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[MENU_NEWS], [MENU_HELP, MENU_ABOUT], [MENU_PING]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Choose a menu item",
)

HELP_TEXT = """Команды:
/start - Главное меню
/news - Последние новости Minecraft
/help - Помощь
/about - О боте
/ping - Проверить работу бота"""

Send a normal text message and the bot will echo it back."""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    # Persist the user in PostgreSQL when available (insert on first contact,
    # refresh otherwise). Without a database the bot still greets the user.
    pool = context.bot_data.get(DB_KEY)
    is_new = True
    if pool is not None:
        is_new = await db.upsert_user(pool, user.id, user.username, user.first_name)

    name = user.first_name if user.first_name else "friend"
    greeting = "Welcome" if is_new else "Welcome back"
    await message.reply_text(
        f"{greeting}, {name}! The bot is running.\n\n"
        "Choose a menu button below or type /help to see the available commands.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return

    await message.reply_text(HELP_TEXT)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return

    await message.reply_text(
        "This bot is built with python-telegram-bot and is ready to deploy on Railway."
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    # Demonstrate a short-lived Redis cache: warm within the TTL, cold otherwise.
    # Without Redis we simply reply with a plain pong.
    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        await message.reply_text("pong")
        return

    cached = await cache.get_or_set_ping(client)
    source = "cached" if cached else "fresh"
    await message.reply_text(f"pong ({source})")


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    if text == MENU_NEWS:
        await news(update, context)
    elif text == MENU_HELP:
        await help_command(update, context)
    elif text == MENU_ABOUT:
        await about(update, context)
    elif text == MENU_PING:
        await ping(update, context)


def _fetch_minecraft_news():
    url = (
        "https://news.google.com/rss/search?"
        "q=site%3Aminecraft.net%2Fen-us%2Farticle%20Minecraft"
        "&hl=en-US&gl=US&ceid=US%3Aen"
    )

    request = Request(
        url,
        headers={
            "User-Agent": "MinecraftTelegramBot/1.0",
            "Accept": "application/rss+xml, application/xml",
        },
    )

    with urlopen(request, timeout=10) as response:
        data = response.read()

    root = ET.fromstring(data)
    articles = []

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()

        title = (
            title.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        link = (
            link.replace("&", "&amp;")
            .replace('"', "&quot;")
        )

        if title and link:
            articles.append((title, link))

        if len(articles) >= 5:
            break

    return articles


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "📰 Ищу последние новости Minecraft..."
    )

    try:
        articles = await asyncio.to_thread(
            _fetch_minecraft_news
        )

    except Exception:
        logger.exception("Failed to fetch Minecraft news")

        await message.reply_text(
            "Не удалось получить новости сейчас 😔\n"
            "Попробуй ещё раз через минуту."
        )

        return

    if not articles:
        await message.reply_text(
            "Пока не нашёл свежие новости 😔"
        )
        return

    text = "📰 <b>Последние новости Minecraft</b>\n\n"

    for i, (title, url) in enumerate(articles, 1):
        text += f'{i}. <a href="{url}">{title}</a>\n\n'

    text += "Источник: Minecraft.net"

    await message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )



    await message.reply_text(f"You sent (#{count}):\n{message.text}")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    # Transient polling/network errors (e.g. a brief 409 Conflict during a
    # Railway redeploy when two instances overlap) are self-healing, so log them
    # as warnings without a traceback instead of alarming-looking errors.
    if isinstance(error, (Conflict, NetworkError, TimedOut)):
        logger.warning("Transient Telegram error: %s", error)
        return

    logger.exception("Error while processing update: %s", update, exc_info=error)

    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Sorry, an error occurred while processing your message."
        )


async def set_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("news", news))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({MENU_NEWS}|{MENU_HELP}|{MENU_ABOUT}|{MENU_PING})$"
            ),
            menu_button,
        )
    )
