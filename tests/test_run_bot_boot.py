"""Boots the real run_bot.py process against a stub Bot API.

Verifies the parts unit tests cannot reach: token handling, getMe validation,
webhook clearing, the polling loop, update routing, the Render health endpoint,
and clean SIGTERM shutdown.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def test_missing_token_exits_with_guidance():
    env = {k: v for k, v in os.environ.items() if k != "BOT_TOKEN"}
    proc = subprocess.run(
        [PYTHON, "run_bot.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "BOT_TOKEN is not set" in proc.stderr
    # The message must point at the dashboard, not at editing source.
    assert "Environment" in proc.stderr


def test_boots_polls_routes_updates_and_stops_cleanly():
    stub_port = _free_port()
    health_port = _free_port()

    stub = subprocess.Popen(
        [PYTHON, str(Path(__file__).parent / "stub_telegram_server.py"), str(stub_port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    bot = None
    try:
        assert _wait_for_port(stub_port), "stub API never came up"

        env = dict(os.environ)
        env.update(
            {
                "BOT_TOKEN": "123456789:AAstub-token-for-tests",
                "TELEGRAM_API_BASE": f"http://127.0.0.1:{stub_port}",
                "PORT": str(health_port),
                "ROUND_SECONDS": "60",
                "LOG_LEVEL": "INFO",
            }
        )
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        bot = subprocess.Popen(
            [PYTHON, "run_bot.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **kwargs,
        )

        assert _wait_for_port(health_port), "health endpoint never bound $PORT"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{health_port}/", timeout=5
        ) as response:
            assert response.status == 200
            assert b"polling" in response.read()

        # Give the loop time to consume the scripted updates.
        deadline = time.monotonic() + 20
        calls: list[str] = []
        while time.monotonic() < deadline:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{stub_port}/__calls", timeout=5
            ) as response:
                calls = json.loads(response.read())["calls"]
            if any(c.startswith("sendMessage:chat=-100999") for c in calls):
                break
            time.sleep(0.5)

        assert "getMe" in calls, "token was never validated"
        assert "deleteWebhook" in calls, "stale webhook was not cleared"
        assert "getUpdates" in calls, "polling never started"
        # /newgame in the group produced the lobby announcement.
        assert any(c == "sendMessage:chat=-100999" for c in calls), calls
        # /start in private produced the DM-ready confirmation.
        assert any(c == "sendMessage:chat=501" for c in calls), calls
        # The join button press was acknowledged.
        assert "answerCallbackQuery" in calls, calls

        if sys.platform == "win32":
            bot.send_signal(signal.CTRL_BREAK_EVENT)
            stdout, _ = bot.communicate(timeout=45)
            assert bot.returncode in (0, 1, 3221225786), stdout
        else:
            bot.send_signal(signal.SIGTERM)
            stdout, _ = bot.communicate(timeout=45)
            assert bot.returncode == 0, stdout
            assert "shutting down" in stdout
        assert "authenticated as @stubbot" in stdout
    finally:
        for proc in (bot, stub):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


if __name__ == "__main__":
    test_missing_token_exits_with_guidance()
    print("ok   test_missing_token_exits_with_guidance")
    test_boots_polls_routes_updates_and_stops_cleanly()
    print("ok   test_boots_polls_routes_updates_and_stops_cleanly")
