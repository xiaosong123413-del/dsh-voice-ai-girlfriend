"""Protected phone UI and CPU voice API. Bind only to loopback behind HTTPS."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
import uuid

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
import cpu_workers
from cpu_runtime import CpuRuntime
from reply_protocol import ReplyStore, ProtocolError

IDENT = r"^[A-Za-z0-9_-]{1,100}$"


class Begin(BaseModel):
    session_id: str = Field(pattern=IDENT)
    bridge_epoch: str
    replaces_reply_id: str | None = Field(default=None, pattern=IDENT)


class TextSegment(BaseModel):
    bridge_epoch: str
    text: str = Field(min_length=1, max_length=24)
    source_message_id: str = Field(min_length=1, max_length=200)


class Finish(BaseModel):
    bridge_epoch: str
    text_count: int = Field(ge=0)
    outcome: str = "completed"
    reason: str | None = None


class Prompt(Begin):
    reply_id: str = Field(pattern=IDENT)
    text: str = Field(min_length=1, max_length=4000)


class Cancel(BaseModel):
    bridge_epoch: str
    reason: str = Field(default="user_cancelled", max_length=100)


def create_app(config, runtime_factory=CpuRuntime):
    password = os.environ.get("AI_COMPANION_PASSWORD", "")
    if len(password) < 16:
        raise RuntimeError("Set AI_COMPANION_PASSWORD to at least 16 characters before startup")
    origin = config["public_origin"].rstrip("/")
    if not origin.startswith("https://") and origin not in ("http://127.0.0.1:8765", "http://localhost:8765"):
        raise ValueError("Public origin requires HTTPS")
    secure = origin.startswith("https://")
    store = ReplyStore()
    runtime = runtime_factory(config, store)
    cookies, attempts, captures, prompts = {}, {}, {}, {}
    web = Path(__file__).parent/"phone"
    root = Path(config["media_root"]).resolve()

    @asynccontextmanager
    async def lifespan(app):
        startup = asyncio.create_task(runtime.start())
        app.state.initialization_task = startup
        try:
            yield
        finally:
            await startup
            await runtime.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.runtime = store, runtime

    def authorized(cookie):
        return cookie is not None and cookies.get(cookie, 0) > time.monotonic()

    def same_origin(headers):
        return headers.get("origin") == origin

    @app.middleware("http")
    async def protect(request, call_next):
        public = request.url.path in ("/voice/login", "/voice/login.css")
        if request.method not in ("GET", "HEAD") and not same_origin(request.headers):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        if not public and not authorized(request.cookies.get("voice_session")):
            if request.url.path in ("/voice", "/voice/"):
                return RedirectResponse("/voice/login", 303)
            return JSONResponse({"error": "authentication_required"}, status_code=401)
        if int(request.headers.get("content-length", "0")) > 1_000_000:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Native same-origin form POSTs must retain Origin for the CSRF check.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; media-src 'self' blob:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.exception_handler(ProtocolError)
    async def protocol_error(request, error):
        return JSONResponse({"error": str(error)}, status_code=error.status)

    @app.get("/voice/login")
    async def login_page():
        return FileResponse(web/"login.html")

    @app.get("/voice/login.css")
    async def login_css():
        return FileResponse(web/"style.css")

    @app.post("/voice/login")
    async def login(request: Request):
        peer = request.client.host
        now = time.monotonic()
        recent = [stamp for stamp in attempts.get(peer, []) if now-stamp < 60]
        attempts[peer] = recent
        if len(recent) >= 6:
            raise HTTPException(429, "Too many attempts; wait one minute")
        recent.append(now)
        from urllib.parse import parse_qs
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(413)
        supplied = parse_qs(body.decode()).get("password", [""])[0]
        if not hmac.compare_digest(supplied.encode(), password.encode()):
            return JSONResponse({"error": "incorrect_password"}, status_code=401)
        token = secrets.token_urlsafe(32)
        cookies.clear()
        async with store.lock:
            for queue in tuple(store.subscribers):
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
        cookies[token] = now + 12*3600
        response = RedirectResponse("/voice/", 303)
        response.set_cookie("voice_session", token, max_age=12*3600, secure=secure,
                            httponly=True, samesite="strict", path="/voice")
        return response

    @app.get("/voice/")
    async def page():
        return FileResponse(web/"index.html")

    @app.get("/voice/static/{filename}")
    async def asset(filename: str):
        if filename not in {"app.js", "capture-worklet.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(web/filename)

    @app.get("/voice/api/health")
    async def health():
        return {"status": runtime.status, "error": runtime.error, "models": runtime.metadata,
                "bridge_epoch": store.epoch, "performance_accepted": False}

    def ready():
        if runtime.status != "ready":
            raise HTTPException(503, "Models are not ready")

    def identifier(value):
        if not re.fullmatch(IDENT, value):
            raise HTTPException(422, "Invalid identifier")

    async def begin_reply(reply_id, body):
        identifier(reply_id)
        ready()
        result, created = await store.begin(reply_id, body.session_id, body.bridge_epoch, body.replaces_reply_id)
        if created:
            old_task = runtime.jobs.get(body.replaces_reply_id)
            if old_task:
                old_task.cancel()
            runtime.begin(reply_id)
        return result, created

    @app.put("/voice/api/replies/{reply_id}")
    async def begin(reply_id: str, body: Begin):
        return (await begin_reply(reply_id, body))[0]

    @app.put("/voice/api/replies/{reply_id}/text/{sequence}")
    async def append(reply_id: str, sequence: int, body: TextSegment):
        return await store.append(reply_id, sequence, body.bridge_epoch, body.text, body.source_message_id)

    @app.post("/voice/api/replies/{reply_id}/finish")
    async def finish(reply_id: str, body: Finish):
        return await store.finish(reply_id, body.bridge_epoch, body.text_count, body.outcome, body.reason)

    @app.post("/voice/api/replies/{reply_id}/cancel")
    async def cancel(reply_id: str, body: Cancel):
        async with store.lock:
            reply = store._reply(reply_id)
            session = reply.session_id
        return await runtime.cancel(session, reply_id, body.bridge_epoch, body.reason)

    @app.post("/voice/api/prompt")
    async def prompt(body: Prompt):
        if body.reply_id in prompts and prompts[body.reply_id] != body.text:
            raise HTTPException(409, 'prompt_id_conflict')
        prompts[body.reply_id] = body.text
        result, created = await begin_reply(body.reply_id, body)
        if created:
            try:
                await runtime.dsh.prompt(body.session_id, body.reply_id, body.text)
            except Exception:
                raise HTTPException(502, "DSH prompt failed")
        return result

    @app.post("/voice/api/stt")
    async def stt(request: Request, capture_id: str):
        ready()
        identifier(capture_id)
        pcm = await request.body()
        if not pcm or len(pcm) % 2 or len(pcm) > 960000:
            raise HTTPException(422, "Expected PCM16 mono 16kHz, 0–30 seconds")
        import hashlib
        digest = hashlib.sha256(pcm).hexdigest()
        existing = captures.get(capture_id)
        if existing:
            if existing[0] != digest:
                raise HTTPException(409, "capture_id_conflict")
            return await asyncio.shield(existing[1])
        if len(captures) >= 1000:
            raise HTTPException(429, "Capture limit reached; restart interaction service")
        async def decode():
            result = await runtime.call("asr", cpu_workers.transcribe, pcm)
            return {**result, "capture_id": capture_id}
        task = asyncio.create_task(decode())
        captures[capture_id] = (digest, task)
        return await asyncio.shield(task)

    @app.websocket("/voice/api/vad")
    async def vad(socket: WebSocket):
        cookie = socket.cookies.get("voice_session")
        if not authorized(cookie) or not same_origin(socket.headers) or runtime.status != "ready":
            await socket.close(code=1008)
            return
        await socket.accept()
        connection = str(uuid.uuid4())
        sequence = 0
        try:
            while True:
                remaining = cookies.get(cookie, 0) - time.monotonic()
                if remaining <= 0:
                    await socket.close(code=1008)
                    break
                data = await asyncio.wait_for(socket.receive_bytes(), remaining)
                if not authorized(cookie) or len(data) != 1024:
                    await socket.close(code=1008)
                    break
                result = await runtime.call("asr", cpu_workers.vad_frame, connection, data)
                await socket.send_json({**result, "sequence": sequence})
                sequence += 1
        except (WebSocketDisconnect, TimeoutError):
            pass
        finally:
            await runtime.call("asr", cpu_workers.vad_frame, connection, None)

    @app.get("/voice/api/events")
    async def events(request: Request):
        snapshot, queue = await store.subscribe()
        async def generate():
            try:
                item = snapshot
                while item is not None:
                    if not authorized(request.cookies.get("voice_session")):
                        return
                    yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                    remaining = cookies.get(request.cookies.get("voice_session"), 0) - time.monotonic()
                    if remaining <= 0:
                        return
                    try:
                        item = await asyncio.wait_for(queue.get(), remaining)
                    except TimeoutError:
                        return
            finally:
                await store.unsubscribe(queue)
        return StreamingResponse(generate(), media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"})

    @app.get("/voice/media/{epoch}/{reply_id}/{filename}")
    async def media(epoch: str, reply_id: str, filename: str):
        identifier(epoch); identifier(reply_id)
        if epoch != store.epoch or not re.fullmatch(r"text-\d+-part-\d+\.mp4", filename):
            raise HTTPException(404)
        relative = f"/voice/media/{epoch}/{reply_id}/{filename}"
        async with store.lock:
            reply = store._reply(reply_id)
            if reply.state in ("cancelled", "failed") or not any(m["url"] == relative for m in reply.media):
                raise HTTPException(404)
        path = (root/epoch/reply_id/filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4")
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    import uvicorn
    uvicorn.run(create_app(config), host="127.0.0.1", port=8765, access_log=False)


if __name__ == "__main__":
    main()
