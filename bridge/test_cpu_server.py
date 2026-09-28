import os
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from cpu_server import create_app

class FakeDsh:
    def __init__(self): self.calls = 0
    async def prompt(self, *args): self.calls += 1
    async def cancel(self, *args): pass

class FakeRuntime:
    def __init__(self, config, store):
        self.store, self.jobs = store, {}
        self.status, self.error, self.metadata = "ready", None, [{"kind":"test-double"}]
        self.dsh = FakeDsh()
    async def start(self): pass
    async def close(self): pass
    def begin(self, reply): pass
    async def cancel(self, session, reply, epoch, reason):
        return await self.store.cancel(reply, epoch, reason)

class HttpProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AI_COMPANION_PASSWORD":"unit-test-only-password"})
        self.env.start()
        self.app = create_app({"public_origin":"http://127.0.0.1:8765",
                               "media_root":self.directory.name}, FakeRuntime)
        self.client = TestClient(self.app).__enter__()
        self.origin = {"Origin":"http://127.0.0.1:8765"}
    def tearDown(self):
        self.client.__exit__(None,None,None); self.env.stop(); self.directory.cleanup()
    def login(self):
        result=self.client.post("/voice/login", data={"password":"unit-test-only-password"},
                                headers=self.origin,follow_redirects=False)
        self.assertEqual(result.status_code,303)
    def test_all_private_surfaces_require_authentication(self):
        for route in ("/voice/api/health","/voice/api/events",
                      "/voice/media/a/b/text-0-part-0.mp4","/voice/static/app.js"):
            self.assertEqual(self.client.get(route).status_code,401)
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/voice/api/vad",headers=self.origin): pass
    def test_origin_and_cookie(self):
        self.assertEqual(self.client.post("/voice/login",data={"password":"unit-test-only-password"}).status_code,403)
        self.login()
        self.assertEqual(self.client.get("/voice/api/health").status_code,200)
        self.assertEqual(self.client.cookies.get("voice_session") is not None,True)
    def test_new_login_revokes_cookie_and_closes_existing_events(self):
        self.login()
        old=self.client.cookies.get("voice_session")
        _,queue=self.client.portal.call(self.app.state.store.subscribe)
        self.login()
        self.assertIsNone(self.client.portal.call(queue.get))
        self.assertEqual(self.client.get("/voice/api/health",headers={"Cookie":"voice_session="+old}).status_code,401)
        self.client.portal.call(self.app.state.store.unsubscribe,queue)
    def test_prompt_retry_does_not_resubmit_llm(self):
        self.login()
        body={"reply_id":"reply1","session_id":"session1","bridge_epoch":self.app.state.store.epoch,
              "replaces_reply_id":None,"text":"hello"}
        for _ in range(2):
            self.assertEqual(self.client.post("/voice/api/prompt",json=body,headers=self.origin).status_code,200)
        self.assertEqual(self.app.state.runtime.dsh.calls,1)
        body["text"]="different"
        self.assertEqual(self.client.post("/voice/api/prompt",json=body,headers=self.origin).status_code,409)
    def test_unpublished_media_and_stale_epoch_rejected(self):
        self.login()
        body={"session_id":"session1","bridge_epoch":"old"}
        self.assertEqual(self.client.put("/voice/api/replies/reply1",json=body,headers=self.origin).status_code,409)
        self.assertEqual(self.client.get("/voice/media/old/reply1/text-0-part-0.mp4").status_code,404)
    def test_cancel_ready_reply(self):
        self.login()
        epoch=self.app.state.store.epoch
        self.client.put("/voice/api/replies/reply1",json={"session_id":"session1","bridge_epoch":epoch},headers=self.origin)
        response=self.client.post("/voice/api/replies/reply1/cancel",json={"bridge_epoch":epoch},headers=self.origin)
        self.assertEqual(response.json()["state"],"cancelled")

if __name__=="__main__": unittest.main()
