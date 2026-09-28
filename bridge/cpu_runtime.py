"""Bounded resident model pipeline. Cancellation discards atomic inference results."""
import asyncio
import concurrent.futures
import multiprocessing
from pathlib import Path
import cpu_workers
from dsh_bridge import DshBridge


class CpuRuntime:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.pools, self.jobs = {}, {}
        self.status, self.error, self.metadata = "loading", None, []
        self.dsh = DshBridge(config, store)

    async def call(self, kind, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pools[kind], function, *args)

    async def start(self):
        try:
            for kind in ("asr", "tts", "avatar"):
                self.pools[kind] = concurrent.futures.ProcessPoolExecutor(
                    max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                    initializer=cpu_workers.initialize, initargs=(kind, self.config))
            self.metadata = await asyncio.gather(*(
                self.call(kind, cpu_workers.ready) for kind in self.pools))
            await self.dsh.start()
            self.status = "ready"
        except Exception:
            self.status, self.error = "failed", "model_or_dsh_initialization_failed"

    def begin(self, reply):
        if reply not in self.jobs:
            task = asyncio.create_task(self._pipeline(reply))
            self.jobs[reply] = task
            task.add_done_callback(lambda _: self.jobs.pop(reply, None))

    async def _pipeline(self, reply):
        queue = asyncio.Queue(maxsize=1)
        root = Path(self.config["media_root"])/self.store.epoch/reply

        async def produce():
            index = 0
            while text := await self.store.next_text(reply, index):
                if not await self.store.live(reply):
                    return
                result = await self.call("tts", cpu_workers.synthesize,
                    text["text"], str(root/f"text-{index}.wav"))
                if not await self.store.live(reply):
                    return
                await queue.put((index, result))
                index += 1
            await queue.put(None)

        producer = asyncio.create_task(produce())
        count = 0
        getter = None
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait((getter, producer), return_when=asyncio.FIRST_COMPLETED)
                if producer in done and producer.exception():
                    getter.cancel()
                    await asyncio.gather(getter, return_exceptions=True)
                    raise producer.exception()
                item = await getter
                if item is None:
                    break
                index, audio = item
                for part in range(audio["parts"]):
                    if not await self.store.live(reply):
                        return
                    filename = f"text-{index}-part-{part}.mp4"
                    result = await self.call("avatar", cpu_workers.render,
                        audio["path"], part, str(root/filename))
                    if not await self.store.publish(reply, index, part, result["duration"],
                        f"/voice/media/{self.store.epoch}/{reply}/{filename}"):
                        return
                count += 1
            await self.store.complete(reply, count)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.store.fail(reply, "cpu_generation_failed")
        finally:
            if getter and not getter.done():
                getter.cancel()
                await asyncio.gather(getter, return_exceptions=True)
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def cancel(self, session, reply, epoch, reason):
        result = await self.store.cancel(reply, epoch, reason)
        task = self.jobs.get(reply)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.dsh.cancel(session, reply)
        return result

    async def close(self):
        for task in tuple(self.jobs.values()):
            task.cancel()
        await asyncio.gather(*tuple(self.jobs.values()), return_exceptions=True)
        await self.dsh.close()
        for pool in self.pools.values():
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
