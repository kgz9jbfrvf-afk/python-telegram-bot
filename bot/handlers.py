"""Telegram update handlers."""

import logging

import asyncio 
import json
import re
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from openai import OpenAI
from telegram import ReplyKeyboardMarkup, Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import cache, db, game_memory, activity


logger = logging.getLogger(__name__)


class HumanMessageFilter(filters.MessageFilter):
    """Reject bots and anonymous/channel senders before any handler runs."""

    def filter(self, message):
        return bool(message.from_user and not message.from_user.is_bot
                    and message.sender_chat is None)


HUMAN_MESSAGES = HumanMessageFilter()

# Keys used to read shared connections from Application.bot_data.
DB_KEY = "db"
REDIS_KEY = "redis"
AI_MEMORY_LIMIT = 12
AI_MEMORY_PREFIX = "redstone:memory:"
REDSTONE_PROMPT = activity.PERSONALITY

PROFILE_MEMORY_PROMPT = """
Ты ведёшь краткую долговременную память об одном конкретном участнике Telegram-группы.

На входе тебе дают текущий профиль и новое сообщение этого участника.

Сохраняй только полезные и относительно устойчивые факты, которые участник явно сообщил о себе:
- имя и предпочтительное обращение;
- питомцев и кому они принадлежат;
- интересы и предпочтения;
- важные игровые факты;
- проекты и другие сведения, которые пригодятся в будущих разговорах.

Например:
"У меня есть пёс Рекс" → "У пользователя есть пёс по имени Рекс."
"Мой любимый биом — вишнёвая роща" → сохранить это предпочтение.

Не делай догадок.
Не смешивай факты разных участников.
Не сохраняй пароли, токены, ключи, платёжные данные и другие секреты.

Если новое сообщение не содержит ничего полезного для долговременной памяти,
верни текущий профиль без изменений.

Верни ТОЛЬКО обновлённый профиль обычным текстом, без пояснений и Markdown.
Профиль должен быть кратким.
"""

BOT_COMMANDS = (
    ("personality", "Характер Редстоуна"),
    ("activity_status", "Режим инициативы"),
    ("activity_on", "Включить инициативу (админ)"),
    ("activity_off", "Выключить инициативу (админ)"),
    ("start", "Show the main menu"),
    ("help", "Show help"),
    ("about", "Show bot information"),
    ("ping", "Check bot status"),
    ("news", "Latest Minecraft news"),
    ("ask", "Ask AI"),
    ("game_memory", "Show game memory: group or me"),
    ("forget_game_memory", "Delete game memory: group or me"),
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
/personality - Характер Редстоуна
/activity_status - Режим инициативы (по умолчанию выключена)
/activity_on - Включить инициативу (администратор)
/activity_off - Выключить инициативу (администратор)
/start - Главное меню
/news - Последние новости Minecraft
/ask - Спросить ИИ
/help - Помощь
/about - О боте
/ping - Проверить работу бота
/game_memory [group|me] - Общая или твоя игровая память
/forget_game_memory [group|me] - Удалить игровую память (общую — только админ)"""

async def get_ai_memory(context, chat_id):
    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        return []

    key = f"{AI_MEMORY_PREFIX}{chat_id}"

    try:
        data = await client.get(key)
        if not data:
            return []

        if isinstance(data, bytes):
            data = data.decode("utf-8")

        return json.loads(data)
    except Exception:
        logger.exception("Failed to load AI memory")
        return []


async def save_ai_memory(context, chat_id, history):
    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        return

    key = f"{AI_MEMORY_PREFIX}{chat_id}"
    history = history[-AI_MEMORY_LIMIT:]

    try:
        await client.set(key, json.dumps(history, ensure_ascii=False))
    except Exception:
        logger.exception("Failed to save AI memory")
        
        

async def get_user_profile(context, chat_id, user_id):
    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        return ""

    key = f"redstone:profile:{chat_id}:{user_id}"

    try:
        data = await client.get(key)
        if not data:
            return ""

        if isinstance(data, bytes):
            data = data.decode("utf-8")

        return data

    except Exception:
        logger.exception("Failed to load user profile")
        return ""


async def save_user_profile(context, chat_id, user_id, profile):
    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        return

    key = f"redstone:profile:{chat_id}:{user_id}"

    try:
        await client.set(key, profile)

    except Exception:
        logger.exception("Failed to save user profile")
        
def has_personal_memory_candidate(message_text: str) -> bool:
    """Select explicit self-statements locally; the model still validates facts.

    Question clauses are ignored, but a separate self-statement in the same
    message can update memory. This intentionally favors skipping ambiguous text.
    """
    personal_statement = re.compile(
        r"\b(?:меня\s+зовут|зови(?:те)?\s+меня|называй(?:те)?\s+меня|"
        r"у\s+меня\s+(?:есть|теперь|больше\s+нет)|"
        r"мой|моя|моё|мое|мои|"
        r"я\s+(?:люблю|обожаю|предпочитаю|увлекаюсь|занимаюсь|"
        r"строю|построил[аи]?|играю|живу|работаю|учусь)|"
        r"my\s+(?:name|favorite|favourite|dog|cat|pet|project|base)\b|"
        r"call\s+me|i\s+(?:have|like|love|prefer|play|live|work|study|"
        r"am\s+building))\b",
        re.IGNORECASE,
    )
    question_start = re.compile(
        r"^(?:как|что|где|когда|почему|зачем|кто|какой|какая|какие|"
        r"можно|можешь|подскажи|расскажи|how|what|where|when|why|"
        r"who|can|could|do|does|is|are)\b",
        re.IGNORECASE,
    )
    for match in re.finditer(r"([^.!?\n]+)([.!?\n]|$)", message_text):
        clause = match.group(1).strip()
        if match.group(2) == "?" or question_start.match(clause):
            continue
        if personal_statement.search(clause):
            return True
    return False


async def update_user_profile(
    context,
    chat_id,
    user_id,
    current_profile,
    message_text,
):
    if context.bot_data.get(REDIS_KEY) is None:
        return current_profile
    if game_memory.SENSITIVE.search(message_text):
        return current_profile
    if not has_personal_memory_candidate(message_text):
        return current_profile

    memory_input = (
        f"Текущий профиль:\n"
        f"{current_profile or 'Пока фактов нет'}\n\n"
        f"Новое сообщение участника:\n"
        f"{message_text}"
    )

    try:
        client = OpenAI()

        response = await asyncio.to_thread(
            client.responses.create,
            model="gpt-6-luna",
            instructions=PROFILE_MEMORY_PROMPT,
            input=memory_input[:6000],
            store=False,
            max_output_tokens=800,
        )

        updated_profile = response.output_text.strip()

        if not updated_profile:
            return current_profile

        updated_profile = updated_profile[:4000]

        if updated_profile != current_profile:
            await save_user_profile(
                context,
                chat_id,
                user_id,
                updated_profile,
            )

        return updated_profile

    except Exception:
        logger.exception("Failed to update user profile")
        return current_profile
        
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

async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None or (update.effective_user and update.effective_user.is_bot):
        return

    prompt = " ".join(context.args).strip()

    if not prompt:
        await message.reply_text(
            "Напиши вопрос после команды.\n"
            "Например: /ask как найти древний город?"
        )
        return

    try:
        game_context = ""
        if message.chat.type in ("group", "supergroup") and update.effective_user:
            game_context = await game_memory.answer_context(
                context, message.chat_id, update.effective_user.id,
            )
        client = OpenAI()

        response = await asyncio.to_thread(
    client.responses.create,
    model="gpt-6-luna",
    instructions=REDSTONE_PROMPT,
    input=prompt[:3000] + game_context[:3000],
    store=False,
    max_output_tokens=1000,
)

        answer = response.output_text.strip()

        if not answer:
            answer = "Не получилось сформировать ответ 😕"

        await message.reply_text(answer)
        
    except Exception:
        logger.exception("OpenAI request failed")

        await message.reply_text(
            "Не удалось получить ответ от ИИ 😔"
        )
async def mention_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None or not message.text:
        return
    if update.effective_user is None or getattr(update.effective_user, "is_bot", False):
        return

    bot_username = context.bot.username

    if not bot_username:
        return

    mention = f"@{bot_username}"

    mention_pattern = re.escape(mention) + r"(?![A-Za-z0-9_])"
    if not re.search(mention_pattern, message.text, re.IGNORECASE):
        await game_memory.observe(update, context)
        await activity.maybe_reply(update, context)
        return

    prompt = re.sub(mention_pattern, "", message.text, count=1, flags=re.IGNORECASE).strip()

    if not prompt:
        await message.reply_text(
            "Позови меня и напиши вопрос 😄"
        )
        return
    user = update.effective_user

    user_name = user.full_name if user else "Неизвестный игрок"
    user_id = user.id if user else 0
    username = f"@{user.username}" if user and user.username else "нет username"
    chat_id = message.chat_id
    history = await get_ai_memory(context, chat_id)
    user_profile = await get_user_profile(context, chat_id, user_id)

    history.append({
    "role": "user",
    "content": (
        f"Сообщение написал участник Telegram:\n"
        f"Имя: {user_name}\n"
        f"Username: {username}\n"
        f"User ID: {user_id}\n\n"
        f"Долговременная память об этом участнике:\n{user_profile or 'Пока фактов нет'}\n\n"
        f"Сообщение: {prompt}"
    )
})

    try:
        game_context = ""
        if getattr(getattr(message, "chat", None), "type", None) in ("group", "supergroup"):
            game_context = await game_memory.answer_context(context, chat_id, user_id)
        client = OpenAI()

        response = await asyncio.to_thread(
            client.responses.create,
            model="gpt-6-luna",
            instructions=REDSTONE_PROMPT + game_context[:3000],
            input=[{"role": item["role"], "content": str(item["content"])[:1000]}
                   for item in history[-AI_MEMORY_LIMIT:]],
            store=False,
            max_output_tokens=1000,
        )

        answer = response.output_text.strip()

        if not answer:
            answer = "Не получилось сформировать ответ 😕"
                   
        history.append({
            "role": "assistant",
            "content": answer
        })

        await save_ai_memory(context, chat_id, history)
        
        await update_user_profile(
    context,
    chat_id,
    user_id,
    user_profile,
    prompt,
)

        await message.reply_text(answer)

    except Exception:
        logger.exception("OpenAI mention request failed")

        await message.reply_text(
            "Не удалось получить ответ от ИИ 😔"
        )

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

    logger.error("Error while processing Telegram update (%s)", type(error).__name__)

    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Sorry, an error occurred while processing your message."
        )


async def set_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def register_handlers(application: Application) -> None:
    for command, callback in (
        ("personality", activity.personality),
        ("activity_status", activity.activity_status),
        ("activity_on", activity.activity_on),
        ("activity_off", activity.activity_off),
        ("game_memory", game_memory.show_memory),
        ("forget_game_memory", game_memory.forget_memory),
        ("news", news), ("ask", ask), ("start", start),
        ("help", help_command), ("about", about), ("ping", ping),
    ):
        application.add_handler(CommandHandler(command, callback, filters=HUMAN_MESSAGES))
    application.add_handler(
        MessageHandler(
            HUMAN_MESSAGES & filters.Regex(
                f"^({MENU_NEWS}|{MENU_HELP}|{MENU_ABOUT}|{MENU_PING})$"
            ),
            menu_button,
        )
    )
    application.add_handler(MessageHandler(HUMAN_MESSAGES & filters.TEXT & ~filters.COMMAND, mention_ai))
    application.add_handler(MessageHandler(HUMAN_MESSAGES & filters.COMMAND, unknown_command))
