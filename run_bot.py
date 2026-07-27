"""Telegram Bot API long-polling runner with HTTP retry and callback codec parsing."""

import asyncio
import html
import os
import sys
import httpx
from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter

BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8879242865:AAEEiKyKaonVIeHu5EOqb-D6xYasYIZQEE4"
)

HELP_GUIDE_HTML = """<b>🕵️‍♂️ دليل لعبة الجاسوس والكلمة السرية (Telegram Spy Game) 💬</b>

<blockquote expandable>
<b>💡 فكرة اللعبة:</b>
لعبة ذكاء وتمويه جماعية ممتعة! يرسل البوت موقعاً سرياً واحداً بالخاص لجميع المواطنين (مثل: 🏥 مستشفى / ✈️ مطار / 🍕 مطعم)، ولكنه يختار <b>لاعباً واحداً ليكون الجاسوس 🕵️</b> (لا يعرف الكلمة السرية!).

<b>🎯 الأهداف وظروف الفوز:</b>
• 👥 <b>المواطنون:</b> طرح أسئلة ذكية في المحادثة لكشف الجاسوس وطرد بالتصويت قبل أن يعرف الكلمة السرية!
• 🕵️ <b>الجاسوس:</b> التظاهر بأنك تعرف الكلمة السرية، الاستماع لأسئلة المنافسين بذكاء لاكتشاف المكان السري، أو إقناع الجميع بطرد مواطن بريء!

<b>📋 خطوات اللعب:</b>
1️⃣ أرسل <code>/newgame</code> واضغط <b>➕ انضمام للعبة</b> في المجموعة.
2️⃣ يرسل البوت الكلمة السرية بالخاص لجميع المواطنين (ويرسل للجاسوس تنبيه أنه هو الجاسوس).
3️⃣ تبدأ الأسئلة والتحقيق المباشر في المجموعة (مثال: "هل تزور هذا المكان بالليل؟").
4️⃣ اضغط <b>🗳️ بدء التصويت على الجاسوس</b> واصوت على المشتبه به بضغطة زر واحدة!
5️⃣ إذا تم طرد الجاسوس، يحصل الجاسوس على فرصة أخيرة لتخمين المكان والفوز!
</blockquote>"""


class TelegramBotRunner:
    """Asyncio runner managing Telegram long polling and callback dispatch."""

    def __init__(self) -> None:
        self.base_url = f"https://api.telegram.org/bot{BOT_TOKEN}"
        self.store = SessionStore()
        self.bot_username = "guessJobot"
        self.adapter = TelegramAdapter(
            store=self.store,
            send_message_fn=self.send_message,
            edit_message_fn=self.edit_message_text,
            edit_markup_fn=self.edit_message_reply_markup,
            bot_username=self.bot_username,
        )
        self.client: httpx.AsyncClient | None = None

    async def send_message(
        self, target_id: int, text: str, reply_markup: dict | None = None, parse_mode: str = "HTML"
    ) -> dict:
        if self.client is None:
            return {}
        payload: dict = {"chat_id": target_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            payload["reply_markup"] = reply_markup

        for attempt in range(3):
            try:
                res = await self.client.post(
                    f"{self.base_url}/sendMessage", json=payload, timeout=10.0
                )
                if res.status_code == 429:
                    retry_after = res.json().get("parameters", {}).get("retry_after", 2)
                    await asyncio.sleep(retry_after)
                    continue
                res.raise_for_status()
                data = res.json()
                if data.get("ok"):
                    return data.get("result", {})
                raise RuntimeError(f"Telegram API error: {data.get('description')}")
            except (httpx.HTTPError, RuntimeError) as err:
                if attempt == 2:
                    raise err
                await asyncio.sleep(1.0)
        return {}

    async def edit_message_text(
        self, target_id: int, message_id: int, text: str, reply_markup: dict | None = None, parse_mode: str = "HTML"
    ) -> dict:
        if self.client is None:
            return {}
        payload: dict = {
            "chat_id": target_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        for attempt in range(3):
            try:
                res = await self.client.post(
                    f"{self.base_url}/editMessageText", json=payload, timeout=10.0
                )
                if res.status_code == 429:
                    retry_after = res.json().get("parameters", {}).get("retry_after", 2)
                    await asyncio.sleep(retry_after)
                    continue
                res.raise_for_status()
                data = res.json()
                if data.get("ok"):
                    return data.get("result", {})
                return {}
            except (httpx.HTTPError, RuntimeError) as err:
                if attempt == 2:
                    raise err
                await asyncio.sleep(1.0)
        return {}

    async def edit_message_reply_markup(
        self, target_id: int, message_id: int, reply_markup: dict | None = None
    ) -> None:
        if self.client is None:
            return
        payload: dict = {"chat_id": target_id, "message_id": message_id}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            res = await self.client.post(
                f"{self.base_url}/editMessageReplyMarkup", json=payload, timeout=10.0
            )
            res.raise_for_status()
        except Exception:
            pass

    async def answer_callback_query(
        self, callback_query_id: str, text: str = "", show_alert: bool = False
    ) -> None:
        if self.client is None:
            return
        try:
            payload = {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            }
            res = await self.client.post(
                f"{self.base_url}/answerCallbackQuery", json=payload, timeout=10.0
            )
            res.raise_for_status()
        except Exception as err:
            print(f"[Error callback query]: {err}", file=sys.stderr)

    async def process_update(self, update: dict) -> None:
        """Process a single Telegram Update."""
        # Handle Callback Queries (Inline Keyboard Buttons)
        callback = update.get("callback_query")
        if callback:
            cb_id = callback.get("id", "")
            from_user = callback.get("from", {})
            user_id = from_user.get("id")
            display_name = (from_user.get("first_name", "") or "").strip() or f"User {user_id}"
            message = callback.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            data = callback.get("data", "")

            if not chat_id or not user_id or not cb_id:
                return

            # Short Callback Codec Parsing: sg:{gid}:{round}:{action}
            parts = data.split(":")
            if len(parts) < 4 or parts[0] != "sg":
                await self.answer_callback_query(
                    cb_id, text="⚠️ زر قديم أو غير معروف.", show_alert=True
                )
                return

            game_id = parts[1]
            round_str = parts[2]
            action = parts[3]

            res = None

            if action == "join":
                res = await self.adapter.handle_join(chat_id, user_id, display_name)
            elif action == "leave":
                res = await self.adapter.handle_leave(chat_id, user_id)
            elif action == "start":
                res = await self.adapter.handle_startgame(chat_id, user_id)
            elif action == "cancel":
                res = await self.adapter.handle_cancelgame(chat_id, user_id, game_id)
            elif action == "openvote":
                res = await self.adapter.handle_start_voting(chat_id, user_id)
            elif action == "spymenu":
                res = await self.adapter.handle_spy_guess_menu(chat_id, user_id, game_id)
            elif action == "ref":
                res = await self.adapter.handle_refresh_panel(chat_id)
            elif action == "newgame":
                res = await self.adapter.handle_newgame(chat_id, user_id, display_name)
            elif len(parts) >= 6 and parts[4] == "vote":
                # sg:{gid}:{r}:{vr}:vote:{target_id}
                try:
                    vote_round = int(parts[3].replace("v", "")) if parts[3].startswith("v") else 1
                    target_id = int(parts[5])
                    res = await self.adapter.handle_spy_vote(
                        chat_id, voter_id=user_id, target_id=target_id, game_id=game_id, vote_round=vote_round
                    )
                except ValueError:
                    pass
            elif action == "spyopt" and len(parts) >= 5:
                # sg:{gid}:{r}:spyopt:{option_index}
                try:
                    option_index = int(parts[4])
                    res = await self.adapter.handle_spy_guess(
                        chat_id, spy_id=user_id, option_index=option_index, game_id=game_id
                    )
                except ValueError:
                    pass

            # Always answer callback query immediately!
            if res is not None:
                toast_text = res.alert_text or ("✅ تم!" if res.ok else "⚠️ تعذر التنفيذ.")
                await self.answer_callback_query(cb_id, text=toast_text, show_alert=res.show_alert)
            else:
                await self.answer_callback_query(cb_id, text="⚠️ انتهت صلاحية هذا الزر.", show_alert=True)
            return

        # Handle Group MyChatMember (Bot added to group -> Send Launcher)
        my_chat_member = update.get("my_chat_member")
        if my_chat_member:
            chat = my_chat_member.get("chat", {})
            chat_id = chat.get("id")
            new_status = my_chat_member.get("new_chat_member", {}).get("status")
            if chat_id and new_status in ("member", "administrator"):
                launcher_buttons = [
                    [{"text": "🎮 إنشاء لعبة جديدة", "callback_data": f"sg:launcher:r1:newgame"}],
                    [{"text": "💬 تفعيل الخاص مع البوت", "url": f"https://t.me/{self.bot_username}"}],
                ]
                await self.send_message(
                    chat_id,
                    "👋 <b>أهلاً بكم! أنا بوت لعبة الجاسوس والكلمة السرية!</b> 🕵️‍♂️\n\nاضغطوا الزر أدناه لبدء لعبة جديدة ممتعة مع أصدقائكم!",
                    reply_markup={"inline_keyboard": launcher_buttons},
                )
                return

        # Handle Messages
        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type", "")
        from_user = message.get("from", {})
        user_id = from_user.get("id")
        display_name = html.escape((from_user.get("first_name", "") or "").strip() or f"User {user_id}")

        if not chat_id or not user_id:
            return

        # Private DM Handling -> Mark user DM Ready!
        if chat_type == "private":
            self.adapter.mark_user_dm_ready(user_id)
            text = message.get("text", "")
            if text in ("/help", "/guide", "/rules"):
                await self.send_message(user_id, HELP_GUIDE_HTML, parse_mode="HTML")
            else:
                welcome_dm = (
                    f"👋 <b>أهلاً بك {display_name}!</b>\n\n"
                    "✅ <b>تم تفعيل الخاص بنجاح!</b>\n"
                    "أنت الآن جاهز لاستلام دورك أو الكلمة السرية بالخاص فور بدء الجولة في المجموعة."
                )
                await self.send_message(user_id, welcome_dm, parse_mode="HTML")
            return

        # Group Commands
        text = message.get("text", "")
        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd_part = parts[0].split("@")[0].lower()

        if cmd_part in ("/help", "/guide", "/rules"):
            await self.send_message(chat_id, HELP_GUIDE_HTML, parse_mode="HTML")
        elif cmd_part in ("/settings", "/status"):
            await self.adapter.handle_refresh_panel(group_chat_id=chat_id)
        elif cmd_part in ("/newgame", "/start"):
            await self.adapter.handle_newgame(group_chat_id=chat_id, user_id=user_id, display_name=display_name)

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=35.0) as client:
            self.client = client
            res = await client.get(f"{self.base_url}/getMe")
            data = res.json()
            if data.get("ok"):
                self.bot_username = data["result"].get("username", "guessJobot")
                self.adapter.bot_username = self.bot_username
                print(f"[+] Bot connected: @{self.bot_username}")

            print("[*] Telegram Spy Game Bot Long Polling started successfully...")

            offset = 0
            cleanup_counter = 0
            while True:
                try:
                    res = await client.get(
                        f"{self.base_url}/getUpdates",
                        params={"offset": offset, "timeout": 30},
                        timeout=40.0,
                    )
                    updates_data = res.json()
                    if not updates_data.get("ok"):
                        await asyncio.sleep(2)
                        continue
                    updates = updates_data.get("result", [])
                    for update in updates:
                        offset = max(offset, update["update_id"] + 1)
                        asyncio.create_task(self.process_update(update))

                    cleanup_counter += 1
                    if cleanup_counter >= 100:
                        cleanup_counter = 0
                        self.store.cleanup_expired(max_age_seconds=3600)

                except Exception as err:
                    print(f"[Polling Error]: {err}", file=sys.stderr)
                    await asyncio.sleep(2)


if __name__ == "__main__":
    runner = TelegramBotRunner()
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        print("\nBot stopped.")
