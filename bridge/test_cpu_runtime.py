import asyncio
import tempfile
import unittest
from pathlib import Path
from cpu_runtime import CpuRuntime
from reply_protocol import ReplyStore
import cpu_workers

class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_tts_per_segment_and_short_tail(self):
        store=ReplyStore()
        with tempfile.TemporaryDirectory() as directory:
            runtime=CpuRuntime({"media_root":directory},store)
            calls=[]
            async def call(kind, function, *args):
                calls.append((kind,args))
                if kind=="tts": return {"path":"fake.wav","parts":2,"duration":2.16}
                return {"duration":2 if args[1]==0 else .16}
            runtime.call=call
            await store.begin("reply","session",store.epoch,None)
            await store.append("reply",0,store.epoch,"你好。","source")
            await store.finish("reply",store.epoch,1)
            await runtime._pipeline("reply")
            self.assertEqual(store.replies["reply"].state,"ready")
            self.assertEqual([kind for kind,_ in calls],["tts","avatar","avatar"])
            self.assertEqual([x["duration"] for x in store.replies["reply"].media],[2,.16])
    async def test_cancel_during_atomic_render_discards_result(self):
        store=ReplyStore()
        with tempfile.TemporaryDirectory() as directory:
            runtime=CpuRuntime({"media_root":directory},store)
            entered,release=asyncio.Event(),asyncio.Event()
            async def call(kind,function,*args):
                if kind=="tts": return {"path":"fake.wav","parts":1,"duration":1}
                entered.set()
                await release.wait()
                return {"duration":1}
            runtime.call=call
            await store.begin("reply","session",store.epoch,None)
            await store.append("reply",0,store.epoch,"你好。","source")
            await store.finish("reply",store.epoch,1)
            job=asyncio.create_task(runtime._pipeline("reply"))
            await entered.wait()
            await store.cancel("reply",store.epoch)
            release.set();await asyncio.wait_for(job,1)
            self.assertEqual(store.replies["reply"].media,[])
    async def test_model_failure_marks_failed_without_hanging_consumer(self):
        store=ReplyStore()
        runtime=CpuRuntime({"media_root":"unused"},store)
        async def call(*args): raise RuntimeError("forced TTS error")
        runtime.call=call
        await store.begin("reply","session",store.epoch,None)
        await store.append("reply",0,store.epoch,"你好。","source")
        await asyncio.wait_for(runtime._pipeline("reply"),1)
        self.assertEqual(store.replies["reply"].state,"failed")
if __name__=="__main__": unittest.main()
