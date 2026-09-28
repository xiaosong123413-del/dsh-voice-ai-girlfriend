"""Real local HTTP/WS/SSE/model acceptance probe; creates no public endpoint."""
import argparse
import asyncio,json,os,secrets,sys,time,uuid
from pathlib import Path
from cpu_server import create_app
import uvicorn,httpx

root = None
report={"status":"RUNNING","stage":"startup","pid":os.getpid(),"checks":{}}
def save(): (root/"summary.json").write_text(json.dumps(report,ensure_ascii=True,indent=2))
class TestServer(uvicorn.Server):
 def __init__(self,*args,**kwargs):
  super().__init__(*args,**kwargs);self.listening=asyncio.Event()
 async def startup(self,sockets=None):
  await super().startup(sockets);self.listening.set()
async def main(args):
 global root
 root = args.output
 root.mkdir(parents=True)
 os.environ["AI_COMPANION_PASSWORD"]=secrets.token_urlsafe(32)
 config=json.loads(args.config.read_text(encoding="utf-8"))
 config["public_origin"]="http://127.0.0.1:8765"
 app=create_app(config)
 server=TestServer(uvicorn.Config(app,host="127.0.0.1",port=8765,access_log=False,log_level="error"))
 serving=asyncio.create_task(server.serve())
 listener=None
 save()
 try:
  await asyncio.wait_for(server.listening.wait(),30)
  async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",trust_env=False,timeout=60) as client:
   report["checks"]["unauthenticated_health"]=(await client.get("/voice/api/health")).status_code==401
   response=await client.post("/voice/login",data={"password":os.environ["AI_COMPANION_PASSWORD"]},
     headers={"Origin":config["public_origin"]})
   assert response.status_code==303
   client.headers["Origin"]=config["public_origin"]
   report["checks"]["page"]=(await client.get("/voice/")).status_code==200
   report["checks"]["loading_is_not_ready"]=(await client.get("/voice/api/health")).json()["status"]=="loading"
   await asyncio.wait_for(app.state.initialization_task,180)
   health=(await client.get("/voice/api/health")).json()
   assert health["status"]=="ready"
   report.update(stage="transport-and-models",models=health["models"]);save()
   from websockets.asyncio.client import connect
   async with connect("ws://127.0.0.1:8765/voice/api/vad",origin=config["public_origin"],
     additional_headers={"Cookie":"voice_session="+client.cookies.get("voice_session")}) as ws:
    await ws.send(bytes(1024))
    vad=json.loads(await ws.recv())
    report["checks"]["websocket_vad_silence"]=vad=={"speech":False,"ended":False,"sequence":0}
   silence=await client.post("/voice/api/stt?capture_id="+str(uuid.uuid4()),content=bytes(32000))
   report["checks"]["stt_silence"]=silence.status_code==200 and silence.json()["text"]==""
   import soundfile as sf
   import numpy as np
   waveform,rate=sf.read(str(args.sample))
   if rate != 16000 or waveform.ndim != 1 or not len(waveform) or not np.isfinite(waveform).all():
    raise ValueError("Acceptance sample must be nonempty finite mono 16 kHz audio")
   pcm=(np.clip(waveform,-1,1)*32767).astype("<i2").tobytes()
   capture=str(uuid.uuid4())
   first=(await client.post("/voice/api/stt?capture_id="+capture,content=pcm)).json()
   repeated=(await client.post("/voice/api/stt?capture_id="+capture,content=pcm)).json()
   report["checks"]["stt_idempotent"]=first==repeated and bool(first["text"])
   report["recognized_text"]=first["text"]
   rid,sid=str(uuid.uuid4()),"voice-http-"+str(uuid.uuid4())
   epoch=health["bridge_epoch"]
   subscribed=asyncio.Event();ready=asyncio.get_running_loop().create_future()
   async def events():
    async with client.stream("GET","/voice/api/events",timeout=180) as response:
     assert response.status_code==200
     async for line in response.aiter_lines():
      if not line.startswith("data: "): continue
      event=json.loads(line[6:])
      if event["event"]=="snapshot": subscribed.set()
      reply=event.get("reply",{})
      if reply.get("reply_id")==rid and reply.get("state") in ("ready","failed"):
       ready.set_result(reply);return
   listener=asyncio.create_task(events())
   await asyncio.wait_for(subscribed.wait(),10)
   prompt={"reply_id":rid,"session_id":sid,"bridge_epoch":epoch,"text":
       "我刚才说的是："+first["text"]+" 请只用一句不超过十个汉字的中文回应，不要调用工具。"}
   started=time.perf_counter()
   response=await client.post("/voice/api/prompt",json=prompt);assert response.status_code==200
   reply=await asyncio.wait_for(ready,180)
   await listener
   assert reply["state"]=="ready" and reply["media"]
   report["checks"]["sse_real_media"]=True
   report["generated_seconds"]=time.perf_counter()-started
   report["reply"]=reply
   url=reply["media"][0]["url"]
   ranged=await client.get(url,headers={"Range":"bytes=0-1023"})
   report["checks"]["range_mp4"]=ranged.status_code==206 and len(ranged.content)==1024 and "video/mp4" in ranged.headers.get("content-type","")
   report["checks"]["cancel_ready"]=(await client.post("/voice/api/replies/"+rid+"/cancel",
       json={"bridge_epoch":epoch})).json()["state"]=="cancelled"
   report["checks"]["cancelled_media_inaccessible"]=(await client.get(url)).status_code==404
   report.update(status="PASS" if all(report["checks"].values()) else "FAIL",stage="complete",
       performance_accepted=False,phone_accepted=False)
 except Exception as exc:
  report.update(status="FAIL",error_type=type(exc).__name__,error=str(exc))
 finally:
  if listener:
   listener.cancel()
   await asyncio.gather(listener,return_exceptions=True)
  save();server.should_exit=True
  await serving
  report["closed"]=True;save()
if __name__=="__main__":
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--config",type=Path,required=True)
 parser.add_argument("--sample",type=Path,required=True)
 parser.add_argument("--output",type=Path,required=True)
 args=parser.parse_args()
 if args.output.exists() or not args.sample.is_file() or not args.config.is_file():
  parser.error("Inputs must exist and output must be a new directory")
 asyncio.run(main(args))
 raise SystemExit(0 if report["status"]=="PASS" else 1)
