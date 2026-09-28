import argparse,asyncio,json,os,time,uuid,sys
from urllib.parse import urlparse
from pathlib import Path
import httpx
from websockets.asyncio.client import connect
parser=argparse.ArgumentParser(description="Verify an existing authenticated HTTPS companion deployment with real models.")
parser.add_argument("--origin",required=True)
parser.add_argument("--output",type=Path,required=True)
args=parser.parse_args()
parsed=urlparse(args.origin)
if parsed.scheme!="https" or not parsed.netloc or parsed.path not in ("","/") or parsed.query or parsed.fragment:
 parser.error("Origin must be an HTTPS origin without path, query or fragment")
if args.output.exists():parser.error("Use a new output file; preserve prior evidence")
if not os.environ.get("AI_COMPANION_PASSWORD"):parser.error("Set AI_COMPANION_PASSWORD privately in the process environment")
origin=args.origin.rstrip("/")
path=args.output
path.parent.mkdir(parents=True,exist_ok=True)
report={"status":"RUNNING","checks":{},"phone_verified":False,"performance_accepted":False}
def save():path.write_text(json.dumps(report,indent=2),encoding="utf-8")
async def main():
 listener=None
 try:
  async with httpx.AsyncClient(base_url=origin,timeout=60,trust_env=False) as client:
   report["checks"]["login_html"]=(await client.get("/voice/login")).status_code==200
   report["checks"]["unauthorized_health"]=(await client.get("/voice/api/health")).status_code==401
   report["checks"]["outside_voice_404"]=(await client.get("/")).status_code==404
   response=await client.post("/voice/login",data={"password":os.environ["AI_COMPANION_PASSWORD"]},headers={"Origin":origin})
   assert response.status_code==303,"Login failed"
   report["checks"]["secure_cookie"]="secure" in response.headers.get("set-cookie","").lower()
   client.headers["Origin"]=origin
   health=(await client.get("/voice/api/health")).json()
   report["health_status"]=health["status"];save()
   assert health["status"]=="ready","Models not ready"
   report["checks"]["authenticated_page"]=(await client.get("/voice/")).status_code==200
   report["checks"]["cross_origin_rejected"]=(await client.post("/voice/api/stt?capture_id="+str(uuid.uuid4()),content=bytes(32000),headers={"Origin":"https://wrong.example"})).status_code==403
   async with connect(origin.replace("https:","wss:")+"/voice/api/vad",origin=origin,proxy=None,
      additional_headers={"Cookie":"voice_session="+client.cookies.get("voice_session")}) as ws:
    await ws.send(bytes(1024));vad=json.loads(await ws.recv())
    report["checks"]["public_websocket"]=vad=={"sequence":0,"speech":False,"ended":False}
   sid="voice-https-"+str(uuid.uuid4());rid=str(uuid.uuid4());epoch=health["bridge_epoch"]
   subscribed=asyncio.Event();done=asyncio.get_running_loop().create_future()
   async def events():
    async with client.stream("GET","/voice/api/events",timeout=180) as response:
     assert response.status_code==200
     async for line in response.aiter_lines():
      if not line.startswith("data: "):continue
      event=json.loads(line[6:])
      if event["event"]=="snapshot":subscribed.set()
      reply=event.get("reply",{})
      if reply.get("reply_id")==rid and reply.get("media") and "first_media_seconds" not in report:
       report["first_media_seconds"]=time.perf_counter()-started;save()
      if reply.get("reply_id")==rid and reply.get("state") in ("ready","failed"):
       done.set_result(reply);return
   listener=asyncio.create_task(events());await asyncio.wait_for(subscribed.wait(),15)
   started=time.perf_counter()
   response=await client.post("/voice/api/prompt",json={"session_id":sid,"reply_id":rid,"bridge_epoch":epoch,
      "text":"只回复四个字：连接成功。不要调用工具。"})
   assert response.status_code==200
   reply=await asyncio.wait_for(done,180);await listener
   assert reply["state"]=="ready" and reply["media"]
   report["checks"]["public_sse_real_media"]=True
   report["generation_seconds"]=time.perf_counter()-started
   media=await client.get(reply["media"][0]["url"],headers={"Range":"bytes=0-1023"})
   report["checks"]["public_range_mp4"]=media.status_code==206 and len(media.content)==1024 and "video/mp4" in media.headers.get("content-type","")
   report["checks"]["media_not_cached"]="no-store" in media.headers.get("cache-control","")
   response=await client.post("/voice/api/replies/"+rid+"/cancel",json={"bridge_epoch":epoch})
   report["checks"]["public_cancel"]=response.json()["state"]=="cancelled"
   report["checks"]["cancelled_media_denied"]=(await client.get(reply["media"][0]["url"])).status_code==404
   report["reply_text"]=reply["text"]
   report["status"]="PASS" if all(report["checks"].values()) else "FAIL"
 except Exception as exc:report.update(status="FAIL",error_type=type(exc).__name__,error=str(exc))
 finally:
  if listener:listener.cancel();await asyncio.gather(listener,return_exceptions=True)
  save()
save();asyncio.run(main())
sys.exit(0 if report["status"]=="PASS" else 1)
