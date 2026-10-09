"""Reviewed local facts; atomic Redis selection, opt-in scheduling, no AI."""
import json
import logging
import re
import secrets
from datetime import datetime, time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
DATA_PATH = Path(__file__).with_name("data") / "minecraft_facts.json"
CATEGORIES = {"mobs", "rare", "redstone", "secrets", "biomes", "structures", "items", "history", "bedrock"}
ENABLED_GROUPS = "redstone:facts:enabled_groups"
MANUAL_SECONDS = 60
SLOT_TTL = 3 * 86400
GRACE_SECONDS = 300
MAX_FACTS = 1000


def load_catalog(path=DATA_PATH):
    """Validate metadata and Telegram limits offline; URLs are reviewed by humans."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FACTS:
        raise ValueError("Fact catalog must contain 1–1000 facts")
    catalog = {}
    texts = set()
    for row in rows:
        fields = {"id", "category", "title", "text", "edition", "version", "source",
                  "source_title", "source_section", "verified_on", "joke"}
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("Invalid fact fields")
        for field, limit in (("id", 80), ("title", 120), ("text", 900), ("source", 500),
                             ("source_title", 150), ("source_section", 150), ("verified_on", 10)):
            if not isinstance(row[field], str) or not 1 <= len(row[field].strip()) <= limit:
                raise ValueError(f"Invalid fact {field}")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", row["id"]) or row["id"] in catalog:
            raise ValueError("Duplicate or invalid fact ID")
        if row["category"] not in CATEGORIES or row["edition"] not in ("Bedrock", "Java", "both"):
            raise ValueError("Invalid category or edition")
        if any(row[f] is not None and (not isinstance(row[f], str) or not 1 <= len(row[f]) <= 200)
               for f in ("version", "joke")):
            raise ValueError("Invalid optional fact text")
        url = urlsplit(row["source"])
        if (url.scheme != "https" or url.hostname not in ("www.minecraft.net", "help.minecraft.net", "feedback.minecraft.net")
                or url.username or url.password or url.port or not url.path
                or (url.hostname == "feedback.minecraft.net" and "/articles/" not in url.path)):
            raise ValueError("Invalid source URL (community suggestions are not evidence)")
        datetime.strptime(row["verified_on"], "%Y-%m-%d")
        normalized = " ".join(row["text"].lower().split())
        if normalized in texts:
            raise ValueError("Duplicate fact text")
        texts.add(normalized)
        catalog[row["id"]] = row
    return catalog


def schedule_config(times="10:00,19:00", timezone="Europe/Warsaw"):
    zone = ZoneInfo(timezone)
    values = times.split(",")
    if len(values) != 2 or len(set(values)) != 2:
        raise ValueError("FACTS_TIMES must contain two different HH:MM times")
    result = []
    for value in values:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("FACTS_TIMES must use HH:MM,HH:MM")
        hour, minute = map(int, value.split(":"))
        result.append(time(hour, minute, tzinfo=zone))
    return tuple(result), zone


def state_keys(chat_id, publication="manual"):
    prefix = f"redstone:facts:{{{chat_id}}}"
    return (prefix + ":mode", prefix + ":used", prefix + ":manual_limit",
            prefix + ":slot:" + publication, prefix + ":last")


# Validate the complete ID list before mutating. Selection and reservation share
# one transaction across all workers. An attempted send consumes the fact.
RESERVE = """
local ids = cjson.decode(ARGV[1])
if #ids == 0 then return {'empty'} end
local current = {}
for _, id in ipairs(ids) do current[id] = true end
local mode = redis.call('GET', KEYS[1])
if ARGV[2] == 'scheduled' then
    if not mode or mode == 'off' then return {'off'} end
    if not redis.call('SET', KEYS[4], 'reserved', 'NX', 'EX', ARGV[3]) then return {'duplicate'} end
else
    if not redis.call('SET', KEYS[3], 'reserved', 'NX', 'EX', ARGV[4]) then return {'limited'} end
end
for _, id in ipairs(redis.call('SMEMBERS', KEYS[2])) do
    if not current[id] then redis.call('SREM', KEYS[2], id) end
end
local chosen = nil
for _, id in ipairs(ids) do
    if redis.call('SISMEMBER', KEYS[2], id) == 0 then chosen = id; break end
end
if not chosen then
    redis.call('DEL', KEYS[2])
    local last = redis.call('GET', KEYS[5])
    chosen = ids[1]
    if #ids > 1 and chosen == last then chosen = ids[2] end
end
redis.call('SADD', KEYS[2], chosen)
redis.call('SET', KEYS[5], chosen)
return {'ok', chosen, mode or 'off'}
"""
# The registry and mode change together (single Redis DB, not Redis Cluster).
SET_MODE = """
redis.call('SET', KEYS[1], ARGV[1])
if ARGV[1] == 'off' then
    redis.call('SREM', KEYS[2], ARGV[2])
else
    redis.call('SADD', KEYS[2], ARGV[2])
end
return 1
"""


def decode(value):
    return value.decode() if isinstance(value, bytes) else value


async def reserve(client, catalog, chat_id, publication=None):
    ids = list(catalog)
    secrets.SystemRandom().shuffle(ids)
    result = await client.eval(RESERVE, 5, *state_keys(chat_id, publication or "manual"),
                               json.dumps(ids), "scheduled" if publication else "manual",
                               SLOT_TTL, MANUAL_SECONDS)
    return tuple(map(decode, result))


def format_fact(fact, scheduled=False):
    heading = "🎲 Факт дня о Minecraft" if scheduled else "🎲 Случайный факт о Minecraft"
    edition = {"both": "Bedrock и Java", "Bedrock": "Bedrock (включая PS5)", "Java": "Java"}[fact["edition"]]
    version = f"; {fact['version']}" if fact["version"] else ""
    joke = f"\n\n💬 Редстоун: «{fact['joke']}»" if fact["joke"] else ""
    return f"{heading}\n\n{fact['title']}\n\n{fact['text']}{joke}\n\n🎮 {edition}{version}\nИсточник: {fact['source']}"


async def send_fact(context, chat_id, publication=None, message=None):
    client = context.bot_data.get("redis")
    try:
        if client is None:
            if message:
                await message.reply_text("Redis недоступен: факты временно отключены, чтобы не потерять историю и лимиты.")
            return
        catalog = context.bot_data["facts_catalog"]
        result = await reserve(client, catalog, chat_id, publication)
        if result[0] != "ok":
            if message and result[0] == "limited":
                await message.reply_text("Факт уже запрошен: подожди 60 секунд между запросами группы.")
            return
        if publication and decode(await client.get(state_keys(chat_id)[0])) != result[2]:
            return  # Administrator switched modes while this job was preparing.
        text = format_fact(catalog[result[1]], scheduled=bool(publication))
        if message:
            await message.reply_text(text, disable_web_page_preview=True)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
    except Exception:
        # Do not release reservations: a timeout may mean Telegram already sent it.
        logger.warning("Minecraft fact publication unavailable")
        if message:
            try:
                await message.reply_text("Не удалось отправить факт. Попробуй позже; резерв сохранён для защиты от дублей.")
            except Exception:
                logger.warning("Minecraft fact error notice unavailable")


async def fact_command(update, context):
    message, user = update.effective_message, update.effective_user
    if (not message or not user or user.is_bot or getattr(message, "sender_chat", None)
            or getattr(update, "edited_message", None) or message.chat.type not in ("private", "group", "supergroup")):
        return
    await send_fact(context, message.chat_id, message=message)


async def mode_command(update, context, enabled=None):
    message, user = update.effective_message, update.effective_user
    if not message or not user or user.is_bot:
        return
    if message.chat.type not in ("group", "supergroup") or getattr(message, "sender_chat", None):
        await message.reply_text("Ежедневные факты настраиваются только в группе.")
        return
    client = context.bot_data.get("redis")
    if client is None:
        await message.reply_text("Redis недоступен: автоматические факты отключены.")
        return
    try:
        if enabled is not None:
            member = await context.bot.get_chat_member(message.chat_id, user.id)
            if member.status not in ("creator", "administrator"):
                await message.reply_text("Включать и выключать факты может только администратор группы.")
                return
            await client.eval(SET_MODE, 2, state_keys(message.chat_id)[0], ENABLED_GROUPS,
                              secrets.token_hex(16) if enabled else "off", str(message.chat_id))
        mode = decode(await client.get(state_keys(message.chat_id)[0]))
        times, zone = context.bot_data["facts_schedule"]
        hours = ", ".join(t.strftime("%H:%M") for t in times)
        await message.reply_text(f"Ежедневные факты {'включены' if mode and mode != 'off' else 'выключены'}. "
                                 f"Расписание: {hours}, {zone.key}. /fact — вручную, один запрос за 60 секунд.")
    except Exception:
        logger.warning("Minecraft facts settings unavailable")
        await message.reply_text("Не удалось проверить права или изменить настройки фактов. Попробуй позже.")


async def facts_on(update, context):
    await mode_command(update, context, True)


async def facts_off(update, context):
    await mode_command(update, context, False)


async def facts_status(update, context):
    await mode_command(update, context)


def publication_id(now, slot, schedule):
    times, zone = schedule
    local = now.astimezone(zone)
    target = datetime.combine(local.date(), times[slot])
    if target.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None) != target.replace(tzinfo=None):
        return None  # A configured wall-clock time does not exist on a DST transition.
    elapsed = (local.astimezone(ZoneInfo("UTC")) - target.astimezone(ZoneInfo("UTC"))).total_seconds()
    if not 0 <= elapsed <= GRACE_SECONDS:
        return None
    return f"{local.date().isoformat()}:{slot}"


async def daily_job(context):
    client = context.bot_data.get("redis")
    if client is None:
        return
    schedule = context.bot_data["facts_schedule"]
    slot = context.job.data
    try:
        async for raw_id in client.sscan_iter(ENABLED_GROUPS, count=100):
            publication = publication_id(datetime.now(ZoneInfo("UTC")), slot, schedule)
            if publication is None:
                return  # No backlog, even if a large registry takes too long.
            try:
                chat_id = int(raw_id)
            except (ValueError, TypeError):
                continue
            await send_fact(context, chat_id, publication)
    except Exception:
        logger.warning("Daily Minecraft facts registry unavailable")


def configure(application, times="10:00,19:00", timezone="Europe/Warsaw"):
    schedule = schedule_config(times, timezone)
    application.bot_data["facts_catalog"] = load_catalog()
    application.bot_data["facts_schedule"] = schedule
    if application.job_queue is None:
        raise RuntimeError("Install python-telegram-bot[job-queue] for daily facts")
    for slot, when in enumerate(schedule[0]):
        application.job_queue.run_daily(daily_job, when, data=slot, name=f"minecraft-facts-{slot}",
                                        job_kwargs={"misfire_grace_time": GRACE_SECONDS,
                                                    "coalesce": True, "max_instances": 1})
