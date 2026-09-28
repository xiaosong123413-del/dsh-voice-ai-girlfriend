"""Native DSH SDK subprocess plus an installed-event streaming plugin."""
import asyncio
import json
import os
import secrets
from pathlib import Path
import httpx
from speech_selection import SpeechSelection


class DshBridge:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.pending, self.turns, self.locks = {}, {}, {}
        self.counter = 0
        self.process = None
        self.control = secrets.token_urlsafe(32)
        self.control_ready = asyncio.Event()
        self.port = None
        self.reader = self.stderr = None

    async def start(self):
        env = dict(os.environ, AI_COMPANION_DSH_CONTROL=self.control,
                   AI_COMPANION_DSH_ENTRY=self.config["dsh_cli"])
        self.process = await asyncio.create_subprocess_exec(
            self.config.get("node", "node"), self.config["dsh_cli"],
            "--profile", "sdk", "--patch", self.config["dsh_patch"],
            cwd=self.config["worktree"], env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8*1024*1024)
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._discard_stderr())
        await self.request("initialize", {"cwd": self.config["worktree"],
            "provider": "deepseek-official", "model": self.config["dsh_model"],
            "maxTokens": self.config.get("max_tokens", 512)})
        await asyncio.wait_for(self.control_ready.wait(), 30)

    async def _discard_stderr(self):
        while await self.process.stderr.read(65536):
            pass  # Do not persist native config/credential diagnostics.

    async def request(self, method, params):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": ident,
            "method": method, "params": params})+"\n").encode())
        await self.process.stdin.drain()
        try:
            return await asyncio.wait_for(future, 60)
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if "id" in item:
                    future = self.pending.get(item["id"])
                    if future and not future.done():
                        if "error" in item:
                            future.set_exception(RuntimeError("DSH RPC rejected request"))
                        else:
                            future.set_result(item.get("result"))
                    continue
                method, params = item.get("method"), item.get("params", {})
                if method == "voice.ready":
                    self.port = params["port"]
                    self.control_ready.set()
                    continue
                turn = self.turns.get(params.get("sessionId"))
                if turn is not None:
                    try:
                        await self._event(turn, method, params)
                    except Exception:
                        await self.store.fail(turn["reply"], "dsh_stream_protocol_failed")
                        if not turn.get("failed"):
                            turn["failed"] = True
                            turn["abort_task"] = asyncio.create_task(self.cancel(params["sessionId"], turn["reply"]))
                            turn["abort_task"].add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("DSH disconnected"))
            for turn in self.turns.values():
                await self.store.fail(turn["reply"], "dsh_disconnected")
                turn["ended"].set()

    async def _append(self, turn, chunks):
        for source, text in chunks:
            if not await self.store.live(turn["reply"]):
                return
            await self.store.append(turn["reply"], turn["sequence"], self.store.epoch, text, source)
            turn["sequence"] += 1

    async def _event(self, turn, method, params):
        if method == "voice.prompt-start":
            turn["armed"] = True
            return
        if not turn.get("armed", True):
            return
        choice = turn["choice"]
        if turn.get("failed"):
            if method == "session.event" and params["event"]["type"] == "turn/end":
                turn["ended"].set()
            return
        if method == "voice.stream":
            frame = params["frame"]
            if frame["type"] == "start":
                turn["attempt"] = str(frame["attemptId"])
                turn["cumulative"] = ""
            elif frame["type"] == "chunk" and frame["chunk"]["type"] == "text-delta":
                text = frame["chunk"]["text"]
                turn["cumulative"] += text
                if turn["retry"] and not choice.first_committed:
                    chunks = choice.retry(turn["attempt"], turn["cumulative"])
                else:
                    chunks = choice.delta(turn["attempt"], text)
                await self._append(turn, chunks)
        elif method == "session.event":
            event = params["event"]
            data = event.get("data", {})
            if event["type"] == "assistant/attempt":
                turn["retry"] = True
            elif event["type"] == "assistant/message":
                message = data["message"]
                text = "".join(block.get("text", "") for block in message.get("content", [])
                               if block.get("type") == "text")
                if turn["retry"] and not choice.first_committed:
                    await self._append(turn, choice.retry(turn["attempt"], text))
                await self._append(turn, choice.commit(turn["attempt"], text))
                turn["retry"] = False
            elif event["type"] == "turn/end":
                reason = data["reason"]["kind"]
                if await self.store.live(turn["reply"]):
                    if reason == "completed":
                        await self._append(turn, choice.finish())
                        await self.store.finish(turn["reply"], self.store.epoch, turn["sequence"])
                    else:
                        await self.store.fail(turn["reply"], "dsh_turn_" + reason)
                turn["ended"].set()

    async def prompt(self, session, reply, text):
        async with self.locks.setdefault(session, asyncio.Lock()):
            await self._cancel(session)
            turn = {"reply": reply, "choice": SpeechSelection(), "sequence": 0,
                    "attempt": "", "cumulative": "", "retry": False, "armed": False, "ended": asyncio.Event()}
            self.turns[session] = turn
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
                    response = await client.post(f"http://127.0.0.1:{self.port}/prompt",
                        headers={"Authorization": "Bearer " + self.control},
                        json={"sessionId": session, "text": text, "cwd": self.config["worktree"],
                              "model": self.config["dsh_model"], "maxTokens": self.config.get("max_tokens", 512)})
                    response.raise_for_status()
            except Exception:
                await self.store.fail(reply, "dsh_prompt_failed")
                turn["ended"].set()
                raise

    async def cancel(self, session, expected_reply=None):
        async with self.locks.setdefault(session, asyncio.Lock()):
            old = self.turns.get(session)
            if expected_reply is not None and (old is None or old['reply'] != expected_reply):
                return
            await self._cancel(session)

    async def _cancel(self, session):
        old = self.turns.get(session)
        if old is None or old["ended"].is_set():
            return
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            response = await client.post(f"http://127.0.0.1:{self.port}/cancel",
                headers={"Authorization": "Bearer " + self.control}, json={"sessionId": session})
            response.raise_for_status()
        await asyncio.wait_for(old["ended"].wait(), 30)

    async def close(self):
        if self.process and self.process.returncode is None:
            try:
                await self.request("shutdown", {})
                await asyncio.wait_for(self.process.wait(), 10)
            except (TimeoutError, RuntimeError, BrokenPipeError):
                self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr):
            if task:
                await asyncio.gather(task, return_exceptions=True)
