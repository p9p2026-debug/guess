"""A throwaway Bot API stub used to boot run_bot.py without touching Telegram.

Serves just enough of the Bot API for the entry point to authenticate, poll a
scripted set of updates, and shut down.  Started as a subprocess by
``test_run_bot_boot.py``; not part of the deployed code.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GROUP = -100999
HOST_ID = 501

#: Delivered on the first getUpdates poll, then never again.
SCRIPTED_UPDATES = [
    {
        "update_id": 1,
        "message": {
            "message_id": 11,
            "chat": {"id": GROUP, "type": "supergroup"},
            "from": {"id": HOST_ID, "first_name": "سالم"},
            "text": "/newgame",
        },
    },
    {
        "update_id": 2,
        "message": {
            "message_id": 12,
            "chat": {"id": HOST_ID, "type": "private"},
            "from": {"id": HOST_ID, "first_name": "سالم"},
            "text": "/start",
        },
    },
    {
        "update_id": 3,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 502, "first_name": "ليان"},
            "message": {"message_id": 13, "chat": {"id": GROUP, "type": "supergroup"}},
            "data": "join_game",
        },
    },
]

calls: list[str] = []
_message_id = 5000


class Handler(BaseHTTPRequestHandler):
    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/__calls":
            self._reply({"calls": calls})
        else:
            self._reply({"ok": False, "description": "unsupported"})

    def do_POST(self) -> None:  # noqa: N802
        global _message_id
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}

        method = self.path.rsplit("/", 1)[-1]
        calls.append(method)

        if method == "getMe":
            self._reply({"ok": True, "result": {"id": 1, "username": "stubbot"}})
        elif method == "deleteWebhook":
            self._reply({"ok": True, "result": True})
        elif method == "getUpdates":
            # Serve the script once; afterwards report no updates so the loop
            # idles instead of replaying.
            if "getUpdates_served" not in calls:
                calls.append("getUpdates_served")
                self._reply({"ok": True, "result": SCRIPTED_UPDATES})
            else:
                self._reply({"ok": True, "result": []})
        elif method in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            _message_id += 1
            calls.append(f"{method}:chat={payload.get('chat_id')}")
            self._reply({"ok": True, "result": {"message_id": _message_id}})
        elif method == "answerCallbackQuery":
            self._reply({"ok": True, "result": True})
        else:
            self._reply({"ok": False, "description": f"stub has no {method}"})

    def log_message(self, *_args) -> None:
        """Quiet."""


def main() -> None:
    port = int(sys.argv[1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
