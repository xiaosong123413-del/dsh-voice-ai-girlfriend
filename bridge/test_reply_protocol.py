import asyncio
import unittest
from reply_protocol import ReplyStore, ProtocolError


class ReplyProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = ReplyStore()
        self.epoch = self.store.epoch
        await self.store.begin("a", "session", self.epoch, None)

    async def rejects(self, awaitable, message):
        with self.assertRaisesRegex(ProtocolError, message):
            await awaitable

    async def test_retry_content_gap_and_conflict(self):
        await self.store.append("a", 0, self.epoch, "你好", "m1")
        await self.store.append("a", 0, self.epoch, "你好", "m1")
        self.assertEqual(len(self.store.replies["a"].texts), 1)
        await self.rejects(self.store.append("a", 0, self.epoch, "不同", "m1"), "conflict")
        await self.rejects(self.store.append("a", 2, self.epoch, "下一句", "m1"), "gap")

    async def test_finish_and_generated_ready_are_separate(self):
        await self.store.append("a", 0, self.epoch, "你好", "m1")
        await self.store.finish("a", self.epoch, 1)
        self.assertIsNone(await self.store.next_text("a", 1))
        self.assertEqual(self.store.replies["a"].state, "draining")
        self.assertTrue(await self.store.publish("a", 0, 0, 1.9, "/voice/media/a/0.mp4"))
        self.assertTrue(await self.store.complete("a", 1))
        self.assertEqual(self.store.replies["a"].state, "ready")
        await self.store.cancel("a", self.epoch)
        self.assertFalse(await self.store.publish("a", 0, 1, .2, "/voice/media/a/1.mp4"))

    async def test_late_begin_cannot_reactivate(self):
        await self.store.begin("b", "session", self.epoch, "a")
        view, created = await self.store.begin("a", "session", self.epoch, None)
        self.assertFalse(created)
        self.assertEqual(view["state"], "cancelled")
        self.assertEqual(self.store.active["session"], "b")
        await self.rejects(self.store.begin("c", "session", self.epoch, "a"), "compare_failed")

    async def test_epoch_and_finish_count(self):
        await self.rejects(self.store.append("a", 0, "old", "你好", "m1"), "stale")
        await self.rejects(self.store.finish("a", self.epoch, 1), "count_mismatch")

    async def test_snapshot_has_no_subscription_gap(self):
        snapshot, queue = await self.store.subscribe()
        self.assertEqual(snapshot["replies"][0]["text_count"], 0)
        await self.store.append("a", 0, self.epoch, "你好", "m1")
        event = await asyncio.wait_for(queue.get(), 1)
        self.assertGreater(event["revision"], snapshot["revision"])
        self.assertEqual(event["reply"]["text_count"], 1)
        await self.store.unsubscribe(queue)

    async def test_cancel_wakes_waiter_and_rejects_late_result(self):
        waiter = asyncio.create_task(self.store.next_text("a", 0))
        await asyncio.sleep(0)
        await self.store.cancel("a", self.epoch)
        self.assertIsNone(await asyncio.wait_for(waiter, 1))
        self.assertFalse(await self.store.publish("a", 0, 0, 1, "/late.mp4"))

    async def test_text_and_media_sequences_are_distinct(self):
        await self.store.append("a", 0, self.epoch, "长音频", "m1")
        for part in range(3):
            await self.store.publish("a", 0, part, 2, f"/{part}.mp4")
        self.assertEqual([m["media_seq"] for m in self.store.replies["a"].media], [0, 1, 2])
        self.assertEqual([m["text_seq"] for m in self.store.replies["a"].media], [0, 0, 0])

    async def test_slow_subscriber_is_disconnected_not_silently_gapped(self):
        _, queue = await self.store.subscribe()
        for index in range(65):
            await self.store.append("a", index, self.epoch, "字", "m1")
        self.assertIsNone(await queue.get())
        self.assertNotIn(queue, self.store.subscribers)


if __name__ == "__main__":
    unittest.main()
