# Telegram Bot with python-telegram-bot

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/python-telegram-bot?referralCode=asepsp&utm_medium=integration&utm_source=template&utm_campaign=generic)

A starter Telegram bot project built with [`python-telegram-bot`](https://python-telegram-bot.org/), environment-based configuration, Docker support, and Railway deployment setup.

![Telegram Bot Demo](img/bot.png)

## Features

- Persistent chat menu buttons after `/start`
- `/start`, `/help`, `/about`, and `/ping` commands
- Echo replies for normal text messages
- Fallback handler for unknown commands
- Error logging
- **PostgreSQL persistence** (asyncpg): users are stored/updated on `/start`
- **Redis integration**: per-user message counter and a short-lived `/ping` cache
- Configuration loaded from a local `.env` file or Railway variables
- Ready to run with Docker and Railway

## Data Stores

### Silent Minecraft group memory

Ordinary group/supergroup text can now create **structured game facts** without
mentioning the bot and without a reply. The feature uses a local candidate filter
and deterministic extraction; it makes **zero OpenAI calls** for ambient messages.
Questions, greetings, negations, ambiguous multi-object statements, long messages,
and messages containing common credential/contact markers are skipped.

Supported concepts include farms, bases, houses, castles, portals, roads/railways,
villages, ancient cities, strongholds, dragons, withers, elytra and diamonds.
Game projects require an explicit Minecraft/Bedrock/Realm/game marker. The parser
recognizes plans, construction, completion, discoveries and achievements in common
Russian/English forms, and `100 64 -200`, `100, 64, -200` or `x=100 y=64 z=-200`
coordinates. An unspecified dimension stays unspecified. Unknown names and
ambiguous phrasing are deliberately not stored; this is not unrestricted NLP.

Examples:

- `Мы построили ферму на 100, 64, -200` → shared group fact.
- `Я строю замок` → the sender's personal game fact.
- `Планирую построить базу` → the sender's plan.
- `Я победил дракона` → the sender's achievement.

Redis keys are independent of existing profiles and conversation history:

- `redstone:game:group:{chat_id}` — shared world facts.
- `redstone:game:player:{chat_id}:{user_id}` — only this player's facts in this group.

Only allowlisted concept/state/dimension values and integer game coordinates are
stored. No ambient message text, names, usernames, project names, contacts or full
transcripts are retained or sent to OpenAI. Telegram IDs are used only to address
the appropriate Redis key. Each list holds at most 40 deduplicated facts and expires
after 90 days without a write. Deduplication, trimming and expiration use one atomic
Redis Lua script (the Redis account must permit `EVAL`). Up to 10 shared and 10 own
facts are supplied to an existing `/ask` or mention request; no other player's
personal game facts are loaded. Recent facts come first and may become outdated.

Commands (in groups only):

| Command | Access |
| --- | --- |
| `/game_memory` or `/game_memory group` | Current members: view shared facts |
| `/game_memory me` | Current members: view their own game facts |
| `/forget_game_memory group` | Administrators/owner: clear shared facts |
| `/forget_game_memory me` | Current members: clear only their own game facts |

Membership is checked with Telegram on every command; failed checks deny access.
These commands never clear another player's facts or the pre-existing conversational
profiles/history. Future ordinary messages can create new facts after deletion.
Missing Redis disables collection; failed Redis operations do not interrupt AI
answers. Memory commands explain unavailability. Messages from bots, channels,
anonymous senders and edited messages are not collected. Menu and command messages
are handled before ambient collection.

To receive ordinary group messages, configure **BotFather → /setprivacy → Disable**
for this bot (or make it an administrator with appropriate group permissions), then
verify delivery in the group. The application cannot change this Telegram setting.
Let members know that structured game facts are collected. No Railway configuration
changes are required or performed by this feature.

Run offline tests in the local virtual environment:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests use `unittest`, `fakeredis` and Lua execution; Telegram/OpenAI calls are mocked.

PostgreSQL and Redis are **optional**. When `DATABASE_URL` / `REDIS_URL` are set the bot
connects on startup; when they are missing, empty, or unreachable it logs a warning and keeps
running with that backend disabled. This makes local testing easy, while Railway just links
the services automatically.

- **PostgreSQL** — a `users` table is created automatically on first run. `/start` inserts
  a new user or refreshes `username`, `first_name`, and `last_seen` for an existing one.
  Without a database, `/start` still greets the user but nothing is persisted.
- **Redis** — `/ping` reports whether the reply came from a 10-second cache (`fresh` vs
  `cached`), and each echoed text message increments a per-user counter. Without Redis,
  `/ping` replies with a plain `pong` and the counter falls back to in-memory (per-process).

Connections are pooled (asyncpg) and reused across updates, then closed cleanly on shutdown.

## Chat Menu Buttons

The bot shows a persistent reply keyboard after `/start` with these buttons:

| Button  | Action                           |
| ------- | -------------------------------- |
| `Help`  | Show available commands          |
| `About` | Show short bot information       |
| `Ping`  | Check whether the bot is running |

Telegram bots cannot display custom buttons before a user starts or messages the bot. The keyboard appears after the bot replies, then stays available in supported Telegram clients.

## Bot Commands

| Command  | Description                      |
| -------- | -------------------------------- |
| `/start` | Show the welcome message         |
| `/help`  | Show available commands          |
| `/about` | Show short bot information       |
| `/ping`  | Check whether the bot is running |


## Project Structure

```text
.
├── bot/
│   ├── __init__.py
│   ├── cache.py        # Redis client and helpers
│   ├── config.py
│   ├── db.py           # PostgreSQL pool and queries
│   ├── handlers.py
│   └── main.py
├── .env
├── .env.example
├── .dockerignore
├── .gitignore
├── Dockerfile
├── LICENSE
├── railway.json
├── README.md
└── requirements.txt
```

## Set Up the Bot Token

1. Create a bot with Telegram `@BotFather`.
2. Copy the bot token.
3. Add the token to `.env`:

## Environment Variables

| Name           | Required | Default | Description                                        |
| -------------- | -------- | ------- | -------------------------------------------------- |
| `BOT_TOKEN`    | Yes      | -       | Bot token from `@BotFather`                        |
| `DATABASE_URL` | No       | -       | PostgreSQL connection string; omit to run without persistence |
| `REDIS_URL`    | No       | -       | Redis connection string; omit to run without caching          |
| `LOG_LEVEL`    | No       | `INFO`  | Logging level, such as `DEBUG`, `INFO`, or `ERROR` |

On Railway, add the **PostgreSQL** and **Redis** plugins to your project and reference their
connection strings from the bot service:

```text
DATABASE_URL=${{ Postgres.DATABASE_URL }}
REDIS_URL=${{ Redis.REDIS_URL }}
```

For local development you can run both with Docker:

```bash
docker run -d --name pg  -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
docker run -d --name red -p 6379:6379 redis:7
```

## Install and Run Locally

Make sure Python 3.10 or newer is installed.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

For Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m bot.main
```

## Run with Docker

```bash
docker build -t telegram-bot .
docker run --env-file .env telegram-bot
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

## RedstoneAI 4.0: характер и инициатива

Ветка 4.0 основана на `main` с объединённым PR #2 (игровая память 3.0).
Редстоун — дружелюбный безумный майнкрафтер, специалист по Bedrock. Юмор
не должен мешать серьёзной помощи или превращаться в личные оскорбления.

Команды `/personality`, `/activity_status`, `/activity_on`, `/activity_off`
добавлены в `/help` и меню команд Telegram. Включать и выключать инициативу
могут только владелец или администратор группы с проверкой Telegram API.
Инициатива **выключена по умолчанию**, настройка хранится отдельно для группы.
Нет фоновых циклов: ответ возможен только после подходящего игрового сообщения.
Бот игнорирует ботов, пересылки, редактирования, ответы на сообщения, упоминания,
личные темы и чувствительные данные. Консервативный локальный фильтр может
пропускать интересные обсуждения; он намеренно не пытается понимать весь чат.

Redis 6+ (рекомендуется Redis 7) атомарно проверяет режим и резервирует 1800 секунд
до запроса OpenAI. Все экземпляры должны использовать один Redis и одну базу.
Перед отправкой резерв проверяется и продлевается ещё на 1800 секунд; ошибки,
пустой ответ и отключение режима не снимают резерв. Повторное включение также
не обнуляет лимит. Запрос ограничен 20 секундами без автоматических повторов,
1000 символами сообщения, 2500 символами игровой памяти и 400 выходными токенами
(включая reasoning). `store=False`. При отсутствии Redis инициатива не работает,
при ошибках OpenAI бот молчит, обычные команды и ответы по запросу сохраняются.
Выключение отменяет ещё не отправленные ответы; уже отправленный Telegram-запрос
отозвать невозможно. Проверка Redis и внешняя отправка Telegram не являются
единой транзакцией. При сбросе/эвикции Redis данные лимита могут быть потеряны:
для нескольких экземпляров нужен общий устойчивый Redis без эвикции этих ключей.

Тема шутки чередуется локально. Хеши последних десяти инициативных ответов
подавляют точные повторы, но смысловое разнообразие зависит от модели.
Новая инициатива не сохраняет переписку или текст ответов. Старые механизмы
истории при упоминании и персональных профилей сохранены для совместимости.

Игровая память извлекается **без OpenAI**: достижения, совместные проекты,
происшествия, игровые предпочтения из ограниченного словаря. Явное название
постройки распознаётся в виде `Я построил замок под названием «Аврора»`.
Хранятся структурированный факт, технические ID источника и пометка владения;
автор сообщения не считается автоматически владельцем чужого события.
Неясные пересказы пропускаются, имена других игроков не угадываются.
Максимум 40 записей на группу/игрока, новые записи доступны 90 дней; при
дальнейших записях просроченные факты удаляются, неактивные ключи истекают.
Существующие ключи и записи 3.0 читаются без миграции. Фильтр секретов —
консервативный набор правил, а не гарантия распознавания любого вида секрета.

Просмотр: `/game_memory group` и `/game_memory me`.
Удаление: `/forget_game_memory group` (администратор) и
`/forget_game_memory me` (своя память). Эти команды не изменяют старые профили.
Production, Railway, секреты и переменные окружения для этой функции менять не нужно.

### Проверки локально

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q bot tests
.venv/bin/python -m pip check
```

### Проверка в тестовой Telegram-группе

1. Проверить `/start`, кнопки меню, `/help`, `/personality`, `/ask` и упоминание.
2. `/activity_status` должен показывать выключенную инициативу. Обычный участник
   не должен иметь права на `/activity_on` и `/activity_off`.
3. Администратором включить режим и написать: «Мы построили необычную ферму
   в Minecraft Bedrock». Возможно одно короткое сообщение; повторные сообщения
   в следующие 30 минут не должны вызывать инициативу. Обычное упоминание
   и `/ask` продолжают отвечать независимо от этого лимита.
4. Проверить личную беседу, сообщения ботов, ответы и пересылки — без инициативы.
5. Написать «Я нашёл элитры», «Я люблю редстоун», «Я погиб в лаве»,
   «Мы построили замок под названием «Аврора»». Проверить разделение `me` и `group`
   у двух участников, затем права и результат удаления.
6. Выключить режим и проверить отсутствие инициативы. Проверить отдельно запуск
   без Redis и ошибки OpenAI в тестовой среде, не затрагивая production.

Для обычных групповых сообщений Telegram должен доставлять их боту (настройка
privacy mode в BotFather или соответствующие права). Код сам эти настройки не меняет.
