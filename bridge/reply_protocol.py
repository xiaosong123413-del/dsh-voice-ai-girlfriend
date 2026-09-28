"""Atomic reply lifecycle and event subscription, independent of transport/model IO."""
import asyncio
import copy
import uuid
from dataclasses import dataclass, field


class ProtocolError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass
class Reply:
    id: str
    session_id: str
    replaces: str | None
    state: str = "accepting"
    texts: list = field(default_factory=list)
    media: list = field(default_factory=list)
    reason: str | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class ReplyStore:
    def __init__(self):
        self.epoch = str(uuid.uuid4())
        self.lock = asyncio.Lock()
        self.replies = {}
        self.active = {}
        self.revision = 0
        self.subscribers = set()

    def _epoch(self, epoch):
        if epoch != self.epoch:
            raise ProtocolError(409, "stale_bridge_epoch")

    def _reply(self, reply_id):
        if reply_id not in self.replies:
            raise ProtocolError(404, "unknown_reply")
        return self.replies[reply_id]

    def _view(self, reply):
        return {"reply_id": reply.id, "session_id": reply.session_id,
                "state": reply.state, "text_count": len(reply.texts),
                "text": "".join(item["text"] for item in reply.texts),
                "media": copy.deepcopy(reply.media), "reason": reply.reason,
                "bridge_epoch": self.epoch}

    def _emit(self, event, reply):
        self.revision += 1
        item = {"event": event, "revision": self.revision, "reply": self._view(reply)}
        for queue in tuple(self.subscribers):
            if queue.full():
                self.subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(item)
        reply.changed.set()

    async def begin(self, reply_id, session_id, epoch, replaces):
        async with self.lock:
            self._epoch(epoch)
            if reply_id in self.replies:
                old = self.replies[reply_id]
                if old.session_id != session_id or old.replaces != replaces:
                    raise ProtocolError(409, "reply_id_conflict")
                return self._view(old), False
            if self.active.get(session_id) != replaces:
                raise ProtocolError(409, "active_reply_compare_failed")
            if replaces is not None:
                old = self._reply(replaces)
                if old.state not in ("cancelled", "failed"):
                    old.state, old.reason = "cancelled", "replaced"
                    self._emit("cancelled", old)
            reply = Reply(reply_id, session_id, replaces)
            self.replies[reply_id] = reply
            self.active[session_id] = reply_id
            self._emit("begun", reply)
            return self._view(reply), True

    async def append(self, reply_id, sequence, epoch, text, source_message_id):
        if not text.strip() or len(text) > 24:
            raise ProtocolError(422, "text_must_be_1_to_24_codepoints")
        async with self.lock:
            self._epoch(epoch)
            reply = self._reply(reply_id)
            content = {"text": text, "source_message_id": source_message_id}
            if sequence < len(reply.texts):
                if sequence < 0 or reply.texts[sequence] != content:
                    raise ProtocolError(409, "text_sequence_conflict")
                return self._view(reply)
            if reply.state != "accepting":
                raise ProtocolError(409, "reply_closed")
            if sequence != len(reply.texts):
                raise ProtocolError(409, "text_sequence_gap")
            if len(reply.texts) >= 256:
                raise ProtocolError(413, "reply_text_limit")
            reply.texts.append(content)
            self._emit("text_accepted", reply)
            return self._view(reply)

    async def finish(self, reply_id, epoch, text_count, outcome="completed", reason=None):
        async with self.lock:
            self._epoch(epoch)
            reply = self._reply(reply_id)
            if text_count != len(reply.texts):
                raise ProtocolError(409, "final_text_count_mismatch")
            if outcome != "completed":
                if reply.state in ("accepting", "draining"):
                    reply.state, reply.reason = "failed", reason or "upstream_failed"
                    self._emit("failed", reply)
                return self._view(reply)
            if reply.state == "accepting":
                reply.state = "draining"
                self._emit("draining", reply)
            elif reply.state not in ("draining", "ready"):
                raise ProtocolError(409, "reply_closed")
            return self._view(reply)

    async def cancel(self, reply_id, epoch, reason="user_cancelled"):
        async with self.lock:
            self._epoch(epoch)
            reply = self._reply(reply_id)
            if reply.state != "cancelled":
                reply.state, reply.reason = "cancelled", reason
                self._emit("cancelled", reply)
            return self._view(reply)

    async def next_text(self, reply_id, index):
        while True:
            async with self.lock:
                reply = self._reply(reply_id)
                if reply.state in ("cancelled", "failed", "ready"):
                    return None
                if index < len(reply.texts):
                    return copy.deepcopy(reply.texts[index])
                if reply.state == "draining":
                    return None
                reply.changed.clear()
                signal = reply.changed
            await signal.wait()

    async def complete(self, reply_id, processed_text_count):
        async with self.lock:
            reply = self._reply(reply_id)
            if reply.state == "draining" and processed_text_count == len(reply.texts):
                reply.state = "ready"
                self._emit("ready", reply)
                return True
            return False

    async def live(self, reply_id):
        async with self.lock:
            return self._reply(reply_id).state in ("accepting", "draining")

    async def publish(self, reply_id, text_sequence, part_index, duration, url):
        async with self.lock:
            reply = self._reply(reply_id)
            if reply.state not in ("accepting", "draining"):
                return False
            media = {"media_seq": len(reply.media), "text_seq": text_sequence,
                     "part_index": part_index, "duration": duration, "url": url}
            reply.media.append(media)
            self._emit("media_ready", reply)
            return True

    async def fail(self, reply_id, reason):
        async with self.lock:
            reply = self._reply(reply_id)
            if reply.state in ("accepting", "draining"):
                reply.state, reply.reason = "failed", reason
                self._emit("failed", reply)

    async def subscribe(self):
        async with self.lock:
            queue = asyncio.Queue(maxsize=64)
            self.subscribers.add(queue)
            snapshot = {"event": "snapshot", "revision": self.revision,
                        "bridge_epoch": self.epoch, "active": dict(self.active),
                        "replies": [self._view(self.replies[r]) for r in self.active.values()]}
            return snapshot, queue

    async def unsubscribe(self, queue):
        async with self.lock:
            self.subscribers.discard(queue)
