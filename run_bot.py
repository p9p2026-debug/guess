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


HELP_GUIDE_HTML = """<b>🕵️‍♂️ دليل لعبة الجاسوس والكلمة السرية (Telegram Spy Game) 💬</b>

<blockquote expandable>
<b>💡 فكرة اللعبة:</b>
لعبة ذكاء وتمويه جماعية ممتعة! يرسل البوت موقعاً سرياً واحداً بالخاص لجميع اللاعبين (مثل: 🏥 مستشفى / ✈️ مطار / 🍕 مطعم)، ولكنه يختار <b>لاعباً واحداً ليكون الجاسوس 🕵️</b> (لا يعرف الكلمة السرية!).

<b>🎯 الأهداف وظروف الفوز:</b>
• 👥 <b>المواطنون:</b> طرح أسئلة ذكية في المجموعة لكشف الجاسوس وطرد بالتصويت قبل أن يعرف الكلمة السرية!
• 🕵️ <b>الجاسوس:</b> التظاهر بأنك تعرف الكلمة السرية، الاستماع لأسئلة المنافسين بذكاء لاكتشاف المكان السري، أو إقناع الجميع بطرد مواطن بريء!

<b>📋 خطوات اللعب:</b>
1️⃣ أرسل <code>/newgame</code> واضغط <b>➕ انضمام للعبة</b> في المجموعة.
2️⃣ يرسل البوت الكلمة السرية بالخاص لجميع المواطنين (ويرسل للجاسوس تنبيه أنه هو الجاسوس).
3️⃣ تبدأ الأسئلة والتحقيق المباشر في المجموعة (مثال: "هل تزور هذا المكان بالليل؟" / "هل تلبس ملابس معينة هناك؟").
4️⃣ اضغط <b>🗳️ بدء التصويت على الجاسوس</b> واصوت على المشتبه به بضغطة زر واحدة!
5️⃣ إذا تم طرد الجاسوس، يحصل الجاسوس على فرصة أخيرة لتخمين المكان والفوز!
</blockquote>

<b>📊 جدول خيارات الفوز والحسم:</b>
<pre>
┌─────────────────────────┬─────────────────┐
│ النتيجة                 │ الفائز باللعبة  │
├─────────────────────────┼─────────────────┤
│ طرد مواطن بريء          │  🕵️ الجاسوس     │
│ طرد الجاسوس + تخمين صح  │  🕵️ الجاسوس     │
│ طرد الجاسوس + تخمين خطأ │  👥 المواطنون   │
└─────────────────────────┴─────────────────┘
</pre>"""



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
        payload: dict = {"chat_id": target_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = await self.client.post(
            f"{self.base_url}/sendMessage", json=payload, timeout=10.0
        )
        res.raise_for_status()
        data = res.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description')}")

    async def send_photo(
        self, target_id: int, photo_file_id: str, caption: str, reply_markup: dict | None = None, parse_mode: str = "HTML"
    ) -> None:
        if self.client is None:
            return
        payload: dict = {
            "chat_id": target_id,
            "photo": photo_file_id,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = await self.client.post(
            f"{self.base_url}/sendPhoto", json=payload, timeout=10.0
        )
        res.raise_for_status()

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

            if data == "start_voting" and chat_id:
                res = await self.adapter.handle_start_voting(group_chat_id=chat_id)
                await self.answer_callback_query(cb_id, text="تم فتح باب التصويت في المجموعة! 🗳️", show_alert=True)
            elif data.startswith("vote:") and chat_id:
                target_id = int(data.split(":", 1)[1])
                res = await self.adapter.handle_spy_vote(
                    group_chat_id=chat_id, voter_id=user_id, target_id=target_id
                )
                toast = "تم تسجيل صوتك بنجاح! 🗳️" if res.ok else "لقد قمت بالتصويت مسبقاً! ⚠️"
                await self.answer_callback_query(cb_id, text=toast, show_alert=True)
            elif data == "spy_guess_menu" and chat_id:
                res = await self.adapter.handle_spy_guess_menu(
                    group_chat_id=chat_id, user_id=user_id
                )
                toast = "اختر التخمين الصحيح للموقع من الأزرار! 💡" if res.ok else "عذراً! هذا الزر مخصص للجاسوس فقط ⚠️"
                await self.answer_callback_query(cb_id, text=toast, show_alert=True)
            elif data.startswith("spy_guess:") and chat_id:
                word_guess = data.split(":", 1)[1]
                res = await self.adapter.handle_spy_guess(
                    group_chat_id=chat_id, spy_id=user_id, word_guess=word_guess
                )
                await self.answer_callback_query(cb_id, text=f"تم إرسال تخمينك: {word_guess}", show_alert=True)
            elif data == "join_game" and chat_id:
                res = await self.adapter.handle_join(
                    group_chat_id=chat_id, user_id=user_id, display_name=display_name
                )
                toast = "تم انضمامك بنجاح! 👥" if res.ok else (
                    "أنت منضم لهذه اللعبة بالفعل! ✅" if res.reason == "already_member"
                    else "اللوبي ممتلئ بحده الأقصى! ⚠️" if res.reason == "lobby_full"
                    else "لا توجد لعبة مفتوحة للانضمام حالياً."
                )
                await self.answer_callback_query(cb_id, text=toast, show_alert=True)
            elif data == "leave_game" and chat_id:
                res = await self.adapter.handle_leave(group_chat_id=chat_id, user_id=user_id)
                toast = "تمت مغادرتك للعبة! 🚪" if res.ok else "أنت لست مشاركاً في اللعبة."
                await self.answer_callback_query(cb_id, text=toast, show_alert=True)
            elif data == "start_game" and chat_id:
                res = await self.adapter.handle_startgame(group_chat_id=chat_id, user_id=user_id)
                toast = "🚀 بدأت اللعبة وتم توزيع الكلمات بالخاص!" if res.ok else (
                    "عذراً! فقط منشئ اللعبة (Host) يمكنه البدء ⚠️" if res.reason == "not_host"
                    else "يلزم وجود لاعبَين اثنين على الأقل لبدء اللعبة! ⚠️" if res.reason == "below_minimum"
                    else "تعذر بدء اللعبة."
                )
                await self.answer_callback_query(cb_id, text=toast, show_alert=True)
            elif data == "cancel_game" and chat_id:
                res = await self.adapter.handle_cancelgame(group_chat_id=chat_id, user_id=user_id)
                toast = "تم إلغاء اللعبة! ❌" if res.ok else "عذراً! فقط منشئ اللعبة يمكنه إلغاء اللعبة ⚠️"
                await self.answer_callback_query(cb_id, text=toast, show_alert=True)
            elif data == "refresh_panel" and chat_id:
                await self.adapter.handle_status(group_chat_id=chat_id)
                await self.answer_callback_query(cb_id, text="تم إظهار لوحة التحكم بالأسفل! 📌", show_alert=False)

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

        # Direct Message handling (Private chat)
        if chat_type == "private":
            await self.send_message(user_id, HELP_GUIDE_HTML, parse_mode="HTML")
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

        if cmd_part in ("/settings", "/status"):
            await self.adapter.handle_status(group_chat_id=chat_id)
        elif cmd_part in ("/newgame", "/start"):
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
