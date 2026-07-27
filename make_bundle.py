#!/usr/bin/env python3
"""Concatenate the whole project into one self-contained handoff file.

Produces ``_PROJECT_BUNDLE.txt``: a single document that another AI (or person)
can be handed with no repository access.  It contains a rebuild header, then
every source file inlined with an explicit path marker, so the tree can be
reconstructed exactly.

Regenerate after any code change:

    python make_bundle.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "_PROJECT_BUNDLE.txt"

#: Emitted in this order so a reader meets the instructions before the code.
FILES = [
    "DEPLOY.md",
    "render.yaml",
    "requirements.txt",
    ".gitignore",
    ".env.example",
    "run_bot.py",
    "photo_guess_game/__init__.py",
    "photo_guess_game/models.py",
    "photo_guess_game/locations.py",
    "photo_guess_game/session_store.py",
    "photo_guess_game/session_manager.py",
    "photo_guess_game/timer_service.py",
    "photo_guess_game/telegram_adapter.py",
    "photo_guess_game/telegram_api.py",
    "tests/test_end_to_end.py",
    "tests/test_run_bot_boot.py",
    "tests/stub_telegram_server.py",
]

SEPARATOR = "=" * 78

HEADER = """\
{sep}
 TELEGRAM SPY GAME BOT -- COMPLETE PROJECT BUNDLE
 لعبة الجاسوس والكلمة السرية
{sep}

WHAT THIS FILE IS
  Every source file of a working, deployable Telegram bot, concatenated into
  one document. No repository access is needed. Recreate the tree exactly as
  the path markers below describe, and it runs.

HOW TO REBUILD
  For each "===== FILE: <path> =====" marker, create that file at that exact
  relative path and paste the block that follows it, up to the next marker.
  Create the directories "photo_guess_game/" and "tests/" as needed.

  Resulting tree:

    <project-root>/
      DEPLOY.md
      render.yaml
      requirements.txt          (intentionally empty of packages)
      .gitignore
      .env.example
      run_bot.py                <- entry point
      photo_guess_game/
        __init__.py
        models.py
        locations.py
        session_store.py
        session_manager.py
        timer_service.py
        telegram_adapter.py
        telegram_api.py
      tests/
        test_end_to_end.py
        test_run_bot_boot.py
        stub_telegram_server.py

HARD REQUIREMENTS
  * Python 3.10 or newer. Verified on 3.10 (compile) and 3.13 (full suite).
  * ZERO third-party dependencies. The Telegram client is built on the standard
    library. Do NOT add python-telegram-bot, httpx, aiogram, requests, or any
    other package, and do not rewrite the transport to use one.
  * The bot token is read from the BOT_TOKEN environment variable. Do NOT
    hardcode it into any file. Set it in the hosting dashboard.

VERIFY BEFORE DEPLOYING
    python -m pytest tests/ -q      -> expect: 11 passed
    python run_bot.py               -> expect: "authenticated as @<username>"

DEPLOYMENT
  Full instructions are the first block below (DEPLOY.md).

  Short version for Render:
    Build Command : python -m compileall -q photo_guess_game run_bot.py
    Start Command : python run_bot.py
    Health Check  : /
    Env var       : BOT_TOKEN = <token from @BotFather>

  Then in @BotFather: /setprivacy -> Disable
  Without this the bot never receives /newgame in groups.

KNOWN LIMITS (by design, do not "fix" silently)
  * Sessions live in memory; a restart loses active games. SessionStore is the
    single seam to swap for Redis/Postgres.
  * Run exactly ONE instance. Two would both long-poll and Telegram would split
    updates between them, breaking the per-group locks.

{sep}
 {count} files -- {lines} lines
{sep}
"""


def main() -> int:
    blocks: list[str] = []
    total_lines = 0
    missing: list[str] = []

    for relative in FILES:
        path = ROOT / relative
        if not path.is_file():
            missing.append(relative)
            continue
        content = path.read_text(encoding="utf-8")
        total_lines += content.count("\n") + 1
        blocks.append(
            f"\n{SEPARATOR}\n===== FILE: {relative} =====\n{SEPARATOR}\n\n{content}"
        )

    if missing:
        raise SystemExit(f"missing files, bundle would be incomplete: {missing}")

    header = HEADER.format(
        sep=SEPARATOR, count=len(FILES), lines=total_lines
    )
    document = header + "".join(blocks) + f"\n{SEPARATOR}\n END OF BUNDLE\n{SEPARATOR}\n"
    OUTPUT.write_text(document, encoding="utf-8")

    size_kb = OUTPUT.stat().st_size / 1024
    print(f"wrote {OUTPUT.name}: {len(FILES)} files, {total_lines} lines, {size_kb:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
