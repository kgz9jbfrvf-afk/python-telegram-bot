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
