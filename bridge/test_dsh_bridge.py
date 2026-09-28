import asyncio
import unittest
from dsh_bridge import DshBridge
from reply_protocol import ReplyStore
from speech_selection import SpeechSelection

class DshEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=ReplyStore()
        await self.store.begin("reply","session",self.store.epoch,None)
        self.bridge=DshBridge({},self.store)
        self.turn={"reply":"reply","choice":SpeechSelection(),"sequence":0,"attempt":"",
                   "cumulative":"","retry":False,"ended":asyncio.Event()}
        self.bridge.turns["session"]=self.turn
    async def frame(self, frame):
        await self.bridge._event(self.turn,"voice.stream",{"frame":frame})
    async def durable(self,kind,data):
        await self.bridge._event(self.turn,"session.event",{"event":{"type":kind,"data":data}})
    async def test_native_events_finish_once_with_tail(self):
        await self.frame({"type":"start","attemptId":"attempt"})
        await self.frame({"type":"chunk","chunk":{"type":"text-delta","text":"没有句号的短尾"}})
        await self.durable("assistant/message",{"message":{"content":[{"type":"text","text":"没有句号的短尾"}]}})
        await self.durable("turn/end",{"reason":{"kind":"completed"}})
        self.assertEqual(self.store.replies["reply"].texts,[{"text":"没有句号的短尾","source_message_id":"attempt"}])
        self.assertEqual(self.store.replies["reply"].state,"draining")
        self.assertTrue(self.turn["ended"].is_set())
    async def test_resume_events_are_ignored_until_new_prompt_marker(self):
        self.turn["armed"]=False
        await self.frame({"type":"start","attemptId":"old"})
        await self.frame({"type":"chunk","chunk":{"type":"text-delta","text":"旧回复。"}})
        await self.durable("turn/end",{"reason":{"kind":"completed"}})
        self.assertEqual(self.store.replies["reply"].texts,[])
        self.assertFalse(self.turn["ended"].is_set())
        await self.bridge._event(self.turn,"voice.prompt-start",{})
        await self.frame({"type":"start","attemptId":"new"})
        await self.frame({"type":"chunk","chunk":{"type":"text-delta","text":"新回复。"}})
        self.assertEqual(self.store.replies["reply"].texts[0]["text"],"新回复。")
    async def test_late_cancel_does_not_cancel_new_turn(self):
        called=False
        async def cancel(session):
            nonlocal called
            called=True
        self.bridge._cancel=cancel
        await self.bridge.cancel("session","older-reply")
        self.assertFalse(called)
    async def test_failed_stream_waits_for_actual_turn_end(self):
        self.turn["failed"]=True
        await self.frame({"type":"chunk","chunk":{"type":"text-delta","text":"迟到内容"}})
        self.assertEqual(self.store.replies["reply"].texts,[])
        self.assertFalse(self.turn["ended"].is_set())
        await self.durable("turn/end",{"reason":{"kind":"cancelled"}})
        self.assertTrue(self.turn["ended"].is_set())

if __name__=="__main__": unittest.main()
