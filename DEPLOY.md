# Deploying the Spy Game bot to Render

Hand this file to whoever (or whatever) does the deployment. Everything needed
is here; no other document is required.

## What this is

A Telegram group game (لعبة الجاسوس والكلمة السرية). One player is secretly the
spy and does not learn the secret location; everyone else receives it by DM.
The group discusses, votes, and the spy gets one final multiple-choice guess.

## Facts that shape the deployment

- **Zero third-party dependencies.** The Bot API client is built on the standard
  library (`urllib` + `asyncio`). `requirements.txt` is intentionally empty.
  Nothing to install, nothing to pin, nothing to break on a release.
- **Long polling, not webhooks.** No public URL or TLS setup is required. The
  process clears any stale webhook at boot, since the two are mutually exclusive.
- **Binds `$PORT` for a health check.** Render terminates a Web Service that
  never listens on `$PORT`, so the process serves `GET /` returning 200 from a
  daemon thread alongside the polling loop.
- **Python 3.10 or newer.** Verified on 3.10 (compile) and 3.13 (full suite).
- **State is in memory.** Sessions do not survive a restart or a second
  instance. See "Limits" below.

## Deploy

### Option A — Blueprint (uses `render.yaml` in this repo)

1. Render Dashboard → **Blueprints** → **New Blueprint Instance** → select this
   repository.
2. Render reads `render.yaml` and prompts for `BOT_TOKEN` (declared
   `sync: false`, so it is never stored in the repo). Paste the token.
3. **Create**.

### Option B — Manual Web Service

Render Dashboard → **New** → **Web Service** → connect this repository, then:

| Setting | Value |
| --- | --- |
| Language / Runtime | Python 3 |
| Build Command | `python -m compileall -q photo_guess_game run_bot.py` |
| Start Command | `python run_bot.py` |
| Health Check Path | `/` |
| Instance Type | Free is sufficient |

Then **Environment** → **Add Environment Variable**:

| Key | Value | Required |
| --- | --- | --- |
| `BOT_TOKEN` | token from [@BotFather](https://t.me/BotFather) | **yes** |
| `PYTHON_VERSION` | `3.13.4` | no |
| `LOG_LEVEL` | `INFO` | no |

The token is read from the environment and is deliberately not accepted from a
source file. This repository is public, so a committed token is readable by
anyone and Telegram revokes tokens it finds exposed. If the token was ever
pasted into a file, a chat, or a commit, run `/revoke` in @BotFather and deploy
with the replacement.

### Verifying the deployment

Logs should show, in order:

```
authenticated as @<your_bot_username>
health endpoint listening on port 10000
polling started (round length 300s)
```

If instead you see `getMe rejected the token`, the token is wrong or revoked.

## Required BotFather settings

The bot cannot work in groups without these:

1. `/setprivacy` → select the bot → **Disable**
   Group privacy mode must be off, otherwise the bot never receives `/newgame`.
2. `/setjoingroups` → **Enable**
3. Add the bot to the group. It does **not** need admin rights.

## How players use it

1. Add the bot to a group, send `/newgame`.
2. Each player presses **➕ انضمام للعبة**.
3. **Each player must open a private chat with the bot and send `/start` first.**
   Telegram forbids a bot from initiating a DM. If any player has not done this,
   role delivery fails and the round rolls back to the lobby with their name
   listed — by design, so a round is never played with a missing role.
4. The host presses **🚀 بدء اللعبة**.

Commands: `/newgame`, `/vote`, `/panel`, `/cancel` (host only), `/help`.

`/panel` re-sends the control panel at the bottom of the chat, which is the
recovery path if the panel has scrolled far up.

## Game rules as implemented

- 3 to 15 players. Exactly one spy, chosen at random.
- Citizens receive the secret location by DM; the spy receives nothing.
- **No round timer.** A round ends by a ballot, or when the host presses
  **🏁 إنهاء الجولة وكشف الجاسوس**.
- A player cannot vote for themselves.
- A ballot tallies once every active player has voted, or earlier when the host
  presses **🔒 إغلاق التصويت وفرز الأصوات** (requires at least one vote). Without
  this, a single player who never votes would stall the round permanently.
- Tie: nobody is eliminated, the round continues.
- Majority hits the spy: the spy gets **one** multiple-choice guess at the
  location. Correct, the spy wins; wrong, the citizens win.
- Majority hits an innocent: the spy wins immediately.

Every keyboard, in every state including after the game ends, carries
**🏠 القائمة الرئيسية** and **🎮 قائمة اللعبة**, so the group always has
something to press.

## Limits to be aware of

- **In-memory state.** A restart, redeploy, or crash loses all active games.
  Render's free tier also idles a service after inactivity. For durable
  sessions, `SessionStore` is the single place to swap for Redis or Postgres.
- **Single instance only.** Two instances would both long-poll and Telegram
  would split updates between them, so per-group locks would not hold. Do not
  scale beyond one instance without moving state out of the process.
- **No cross-restart timers.** Round deadlines live in the event loop.

## Running locally

```bash
export BOT_TOKEN='123456789:AA...'   # or copy .env.example to .env
python run_bot.py
```

## Tests

```bash
python -m pytest tests/ -q          # 11 tests
python tests/test_end_to_end.py     # also runs without pytest
```

`tests/test_end_to_end.py` drives the real adapter/manager/timer wiring with a
fake HTTP boundary. `tests/test_run_bot_boot.py` launches `run_bot.py` as a real
subprocess against a stub Bot API and asserts it authenticates, polls, routes
updates, serves the health endpoint, and exits 0 on `SIGTERM`.
