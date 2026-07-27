"""Telegram Bot Runner for Photo Guess Game with Interactive Inline Buttons.

Connects Telegram Bot API long-polling updates and button callback queries to TelegramAdapter.
"""

from __future__ import annotations

import asyncio
import os
import sys
import httpx

from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter

BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8879242865:AAEEiKyKaonVIeHu5EOqb-D6xYasYIZQEE4"
)


HELP_GUIDE_HTML = """<b>🎮 دليل لعبة تخمين الصور (Hedbanz / Heads Up) 📸</b>

<blockquote expandable>
<b>💡 فكرة اللعبة:</b>
لعبة جماعية ممتعة تلعبها مع أصدقائك في مجموعات التيليجرام! يقوم كل لاعب برفع صوره الخاصة للبوت بالسرية، ثم يوزع البوت الصور على الجميع بأسماء مستعارة (مثل <code>Photo A</code>, <code>Photo B</code>). هدفك هو التخمين من صاحب كل صورة وكسب النقاط!
</blockquote>

<b>📋 خطوات اللعب وسير الجولة:</b>

<b>1️⃣ فتح اللوبي:</b> أرسل <code>/newgame</code> في المجموعة أو اضغط على زر <b>➕ Join Game</b>.
<b>2️⃣ إرسال الصور:</b> يدخل كل لاعب في المحادثة الخاصة مع البوت <b>@guessJobot</b> ويرسل صورته بالسرية.
<b>3️⃣ بدء اللعبة:</b> يقوم منشئ اللعبة (Host) بالضغط على زر <b>🚀 Start Game</b>.
<b>4️⃣ مرحلة التخمين:</b> تصلك صور باقي اللاعبين في الخاص مرفقة بأزرار فيها أسماء اللاعبين. اضغط على زر اسم اللاعب لتسجيل تخمينك!
<b>5️⃣ إعلان النتائج والترتيب:</b> عند انتهاء الوقت، يظهر كشف الأسرار ولوحة الترتيب النهائية في المجموعة!

<b>📊 نظام النقاط وحساب الترتيب:</b>

<pre>
┌───────────────────┬────────┐
│ النتيجة           │ النقاط │
├───────────────────┼────────┤
│ تخمين صحيح        │  +1    │
│ تخمين خاطئ        │   0    │
│ عدم التخمين       │   0    │
└───────────────────┴────────┘
</pre>

<b>⚡ الأزرار التفاعلية والتحكم:</b>
• <b>➕ Join Game</b> — الانضمام للعبة
• <b>🚪 Leave Game</b> — المغادرة
• <b>🚀 Start Game</b> — بدء الجولة (لـ Host)
• <b>⚙️ /settimeout</b> — ضبط وقت التخمين بالدقائق

<b>💬 نصيحة للنصر:</b>
<blockquote>
ناقش أصدقاءك في المجموعة واسألهم أسئلة ذكية لتكشف صاحب كل صورة!
</blockquote>"""


class TelegramBotRunner:
    """Long-polling bot runner connecting Telegram API to TelegramAdapter."""

    def __init__(self, token: str = BOT_TOKEN) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.store = SessionStore()
        self.client: httpx.AsyncClient | None = None
        self.bot_username = "guessJobot"

        self.adapter = TelegramAdapter(
            store=self.store,
            send_message_fn=self.send_message,
            send_photo_fn=self.send_photo,
        )

    async def send_message(
        self, target_id: int, text: str, reply_markup: dict | None = None, parse_mode: str = "HTML"
    ) -> None:
        if self.client is None:
            return
        try:
            payload: dict = {"chat_id": target_id, "text": text, "parse_mode": parse_mode}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            await self.client.post(
                f"{self.base_url}/sendMessage", json=payload, timeout=10.0
            )
        except Exception as err:
            print(f"[Error sending message to {target_id}]: {err}", file=sys.stderr)

    async def send_photo(
        self, target_id: int, photo_file_id: str, caption: str, reply_markup: dict | None = None, parse_mode: str = "HTML"
    ) -> None:
        if self.client is None:
            return
        try:
            payload: dict = {
                "chat_id": target_id,
                "photo": photo_file_id,
                "caption": caption,
                "parse_mode": parse_mode,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            await self.client.post(
                f"{self.base_url}/sendPhoto", json=payload, timeout=10.0
            )
        except Exception as err:
            print(f"[Error sending photo to {target_id}]: {err}", file=sys.stderr)

    async def answer_callback_query(
        self, callback_query_id: str, text: str = ""
    ) -> None:
        if self.client is None:
            return
        try:
            payload = {"callback_query_id": callback_query_id, "text": text}
            await self.client.post(
                f"{self.base_url}/answerCallbackQuery", json=payload, timeout=10.0
            )
        except Exception as err:
            print(f"[Error answering callback query]: {err}", file=sys.stderr)

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=35.0) as client:
            self.client = client
            res = await client.get(f"{self.base_url}/getMe")
            data = res.json()
            if data.get("ok"):
                self.bot_username = data["result"].get("username", "guessJobot")
                first_name = data["result"].get("first_name", "Bot")
                print(f"[+] Bot connected: {first_name} (@{self.bot_username})")

            print("[*] Starting Telegram Photo Guess Game Bot polling with Rich Text & Interactive Buttons...")

            offset = 0
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
                        await self.process_update(update)

                except asyncio.CancelledError:
                    print("Bot polling cancelled.")
                    break
                except Exception as err:
                    print(f"[Polling Error]: {err}", file=sys.stderr)
                    await asyncio.sleep(2)

    async def process_update(self, update: dict) -> None:
        # Handle Callback Queries (Inline Keyboard Buttons)
        callback = update.get("callback_query")
        if callback:
            cb_id = callback.get("id", "")
            from_user = callback.get("from", {})
            user_id = from_user.get("id")
            display_name = (
                (from_user.get("first_name", "") or "")
                + (
                    " " + (from_user.get("last_name", "") or "")
                    if from_user.get("last_name")
                    else ""
                )
            ).strip() or from_user.get("username") or f"User {user_id}"

            message = callback.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            data = callback.get("data", "")

            if data == "answer:yes" and chat_id:
                res = await self.adapter.handle_answer(
                    group_chat_id=chat_id, responder_id=user_id, answer_type="yes"
                )
                await self.answer_callback_query(cb_id, text="تم تسجيل إجابة: 🟢 نعم")
            elif data == "answer:no" and chat_id:
                res = await self.adapter.handle_answer(
                    group_chat_id=chat_id, responder_id=user_id, answer_type="no"
                )
                await self.answer_callback_query(cb_id, text="تم تسجيل إجابة: 🔴 لا")
            elif data == "guess_intent" and chat_id:
                res = await self.adapter.handle_guess_intent(
                    group_chat_id=chat_id, user_id=user_id
                )
                await self.answer_callback_query(cb_id, text="🎯 أرسل تخمينك بـ /guess الكلمة")
            elif data == "join_game" and chat_id:
                res = await self.adapter.handle_join(
                    group_chat_id=chat_id, user_id=user_id, display_name=display_name
                )
                toast = "تم الانضمام للعبة! 👥" if res.ok else (
                    "أنت منضم مسبقاً!" if res.reason == "already_member"
                    else "اللوبي ممتلئ!" if res.reason == "lobby_full"
                    else "تعذر الانضمام."
                )
                await self.answer_callback_query(cb_id, text=toast)

            elif data == "leave_game" and chat_id:
                res = await self.adapter.handle_leave(group_chat_id=chat_id, user_id=user_id)
                toast = "تم مغادرة اللعبة!" if res.ok else "أنت لست في اللعبة."
                await self.answer_callback_query(cb_id, text=toast)
            elif data == "start_game" and chat_id:
                res = await self.adapter.handle_startgame(group_chat_id=chat_id, user_id=user_id)
                toast = "جاري بدء اللعبة... ⏱️" if res.ok else (
                    "فقط منشئ اللعبة يمكنه البدء!" if res.reason == "not_host"
                    else "عدد اللاعبين غير كافٍ!" if res.reason == "below_minimum"
                    else "هناك لاعبون لم يرسلوا صورهم!" if res.reason == "missing_photos"
                    else "تعذر بدء اللعبة."
                )
                await self.answer_callback_query(cb_id, text=toast)
            elif data == "cancel_game" and chat_id:
                res = await self.adapter.handle_cancelgame(group_chat_id=chat_id, user_id=user_id)
                toast = "تم إلغاء اللعبة!" if res.ok else "فقط منشئ اللعبة يمكنه الإلغاء!"
                await self.answer_callback_query(cb_id, text=toast)
            elif data.startswith("guess:"):
                parts = data.split(":", 2)
                if len(parts) == 3:
                    label, target_id_str = parts[1], parts[2]
                    candidates = self.store.group_chat_ids_for_user(user_id)
                    gid = chat_id or (next(iter(candidates)) if candidates else None)
                    if gid:
                        res = await self.adapter.handle_guess(
                            group_chat_id=gid, guesser_id=user_id, text_args=f"{label} {target_id_str}"
                        )
                        toast = f"تم تسجيل تخمينك لـ {label}! 🎯" if res.ok else (
                            "لا يمكنك تخمين صورتك الخاصة!" if res.reason == "self_guess"
                            else "تخمين غير صحيح."
                        )
                        await self.answer_callback_query(cb_id, text=toast)
            elif data.startswith("disambiguate:"):
                chosen_gid_str = data.split(":", 1)[1]
                res = await self.adapter.handle_dm_text_reply(user_id=user_id, text=chosen_gid_str)
                toast = "تم حفظ الصورة للمجموعة المختارة! 📸" if res.ok else "اختيار غير صحيح."
                await self.answer_callback_query(cb_id, text=toast)
            return


        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type", "")
        from_user = message.get("from", {})
        user_id = from_user.get("id")
        display_name = (
            (from_user.get("first_name", "") or "")
            + (
                " " + (from_user.get("last_name", "") or "")
                if from_user.get("last_name")
                else ""
            )
        ).strip() or from_user.get("username") or f"User {user_id}"

        if not chat_id or not user_id:
            return

        # Direct Message handling
        if chat_type == "private":
            photos = message.get("photo")
            if photos and isinstance(photos, list):
                best_photo = max(photos, key=lambda p: p.get("file_size", 0))
                file_id = best_photo.get("file_id")
                if file_id:
                    await self.adapter.handle_dm_photo(
                        user_id=user_id, file_id=file_id
                    )
                return

            text = message.get("text", "")
            if text and not text.startswith("/"):
                await self.adapter.handle_dm_text_reply(user_id=user_id, text=text)
                return

        # Group chat command handling
        text = message.get("text", "")
        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd_part = parts[0].split("@")[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd_part in ("/help", "/guide", "/rules"):
            await self.send_message(chat_id, HELP_GUIDE_HTML, parse_mode="HTML")
            return

        if cmd_part in ("/newgame", "/start"):
            if chat_type == "private" and cmd_part == "/start":
                await self.send_message(user_id, HELP_GUIDE_HTML, parse_mode="HTML")
                return
            await self.adapter.handle_newgame(
                group_chat_id=chat_id, user_id=user_id, display_name=display_name
            )
        elif cmd_part == "/join":
            await self.adapter.handle_join(
                group_chat_id=chat_id, user_id=user_id, display_name=display_name
            )
        elif cmd_part == "/leave":
            await self.adapter.handle_leave(group_chat_id=chat_id, user_id=user_id)
        elif cmd_part == "/startgame":
            await self.adapter.handle_startgame(group_chat_id=chat_id, user_id=user_id)
        elif cmd_part == "/cancelgame":
            await self.adapter.handle_cancelgame(
                group_chat_id=chat_id, user_id=user_id
            )
        elif cmd_part == "/settimeout":
            await self.adapter.handle_settimeout(
                group_chat_id=chat_id, user_id=user_id, minutes_str=args
            )
        elif cmd_part == "/guess":
            await self.adapter.handle_guess(
                group_chat_id=chat_id, guesser_id=user_id, text_args=args
            )



if __name__ == "__main__":
    runner = TelegramBotRunner()
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
