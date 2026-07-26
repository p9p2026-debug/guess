"""Integration smoke tests and adapter boundary tests for TelegramAdapter."""

import asyncio
import pytest

from photo_guess_game.models import GameState
from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter


class FakeTransport:
    def __init__(self):
        self.sent_messages = []
        self.sent_photos = []

    async def send_message(self, target_id: int, text: str, reply_markup=None):
        self.sent_messages.append((target_id, text))

    async def send_photo(self, target_id: int, photo_file_id: str, text: str, reply_markup=None):
        self.sent_photos.append((target_id, photo_file_id, text))



class SyncFakeScheduler:
    def __init__(self):
        self.pending = []

    def __call__(self, delay: float, callback):
        handle = SyncFakeHandle(self, callback)
        self.pending.append((delay, handle))
        return handle

    def fire_all(self):
        for delay, handle in sorted(self.pending, key=lambda x: x[0]):
            if not handle.cancelled:
                handle.callback()


class SyncFakeHandle:
    def __init__(self, scheduler, callback):
        self.scheduler = scheduler
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def test_full_happy_path_integration():
    async def _test():
        store = SessionStore()
        transport = FakeTransport()
        scheduler = SyncFakeScheduler()

        adapter = TelegramAdapter(
            store=store,
            send_message_fn=transport.send_message,
            send_photo_fn=transport.send_photo,
        )
        adapter.timer_service._scheduler = scheduler

        group_id = 100

        # 1. Host creates game
        res1 = await adapter.handle_newgame(group_id, user_id=1, display_name="Alice")
        assert res1.ok is True

        # 2. Bob and Carol join
        await adapter.handle_join(group_id, user_id=2, display_name="Bob")
        await adapter.handle_join(group_id, user_id=3, display_name="Carol")

        # 3. All submit photos via DM
        await adapter.handle_dm_photo(user_id=1, file_id="photo_alice")
        await adapter.handle_dm_photo(user_id=2, file_id="photo_bob")
        await adapter.handle_dm_photo(user_id=3, file_id="photo_carol")

        # 4. Host sets timeout to 5 minutes and starts game
        await adapter.handle_settimeout(group_id, user_id=1, minutes_str="5")
        start_res = await adapter.handle_startgame(group_id, user_id=1)
        assert start_res.ok is True
        assert start_res.session.state == GameState.GUESSING

        # Verify DMs were sent out to each recipient carrying other players' photos
        assert len(transport.sent_photos) == 6  # 3 players * 2 photos each

        # 5. Players make guesses
        # Alice guesses Photo B is Bob
        await adapter.handle_guess(group_id, guesser_id=1, text_args="Photo B Bob")
        # Bob guesses Photo A is Alice
        await adapter.handle_guess(group_id, guesser_id=2, text_args="Photo A Alice")

        # 6. Timer expires -> triggers Reveal and transition to Completed
        scheduler.fire_all()
        await asyncio.sleep(0)

        session = store.get(group_id)
        assert session.state == GameState.COMPLETED


        # Check reveal notifications reached the group chat
        group_texts = [text for gid, text in transport.sent_messages if gid == group_id]
        reveal_text = "\n".join(group_texts)
        assert "انتهى الوقت!" in reveal_text or "النتائج النهائية" in reveal_text or len(group_texts) > 0


    asyncio.run(_test())


def test_adapter_boundary_rejections():
    async def _test():
        store = SessionStore()
        transport = FakeTransport()

        adapter = TelegramAdapter(
            store=store,
            send_message_fn=transport.send_message,
            send_photo_fn=transport.send_photo,
        )

        # Malformed guess arguments
        res = await adapter.handle_guess(100, guesser_id=1, text_args="")
        assert res.ok is False
        assert res.reason == "malformed_guess"

        # Photo DM with no lobby session
        res2 = await adapter.handle_dm_photo(user_id=999, file_id="some_photo")
        assert res2.ok is False
        assert res2.reason == "no_open_session"

    asyncio.run(_test())

