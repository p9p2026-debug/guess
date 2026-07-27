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

        # 4. Host starts game
        start_res = await adapter.handle_startgame(group_id, user_id=1)
        assert start_res.ok is True
        assert start_res.session.state == GameState.GUESSING
        assert start_res.session.spy_user_id in (1, 2, 3)

        # Verify DM notifications were sent to all 3 players with their secret words
        dm_messages = [msg for gid, msg in transport.sent_messages if gid != group_id]
        assert len(dm_messages) >= 3

        # 5. Start voting and record votes
        await adapter.handle_start_voting(group_id)
        spy_id = start_res.session.spy_user_id
        await adapter.handle_spy_vote(group_id, voter_id=1, target_id=spy_id)
        await adapter.handle_spy_vote(group_id, voter_id=2, target_id=spy_id)
        res_vote = await adapter.handle_spy_vote(group_id, voter_id=3, target_id=spy_id)
        assert res_vote.ok is True

        # Check reveal/voting notifications reached group
        group_texts = [text for gid, text in transport.sent_messages if gid == group_id]
        assert len(group_texts) > 0

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

