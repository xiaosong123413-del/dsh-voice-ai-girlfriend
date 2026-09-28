const fs=require("node:fs");
const vm=require("node:vm");
const assert=require("node:assert/strict");
const {test}=require("node:test");

function browser() {
  const nodes=new Map(), events={}, requests=[], plays=[], sources=[];
  class Element {
    constructor(id="") {this.id=id;this.hidden=false;this.disabled=false;this.value="";this.attrs={};this.listeners={};this.children=[];this.textContent="";}
    addEventListener(name,fn){this.listeners[name]=fn;}
    setAttribute(key,value){this.attrs[key]=value;}
    getAttribute(key){return this.attrs[key]??null;}
    removeAttribute(key){delete this.attrs[key];}
    set src(value){this.attrs.src=value;}
    get src(){return this.attrs.src;}
    load(){}
    pause(){this.paused=true;}
    play(){this.paused=false;plays.push(this.src);return Promise.resolve();}
    append(...children){this.children.push(...children);}
    scrollIntoView(){}
  }
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);};
  get("mode").value="queue";
  let n=0;
  const sandbox={
    console,Int16Array,Map,Set,Promise,Error,JSON,String,
    crypto:{randomUUID:()=>"id-"+(++n)},
    sessionStorage:{getItem:()=>null,setItem:()=>{}},
    location:{origin:"https://example.test"},
    document:{hidden:false,getElementById:get,createElement:()=>new Element(),
      addEventListener:(name,fn)=>{events[name]=fn;}},
    window:{addEventListener:(name,fn)=>{events[name]=fn;}},
    navigator:{mediaDevices:{getUserMedia:async()=>{throw new Error("denied");}}},
    fetch:async(url,options={})=>{
      const body=options.body?JSON.parse(options.body):undefined;
      requests.push({url,body});
      const data=url.endsWith("/health")?{status:"ready",bridge_epoch:"epoch"}:
        url.endsWith("/prompt")?{reply_id:body.reply_id}:{state:"cancelled"};
      return {ok:true,status:200,headers:{get:()=>"application/json"},json:async()=>data};
    },
    EventSource:class {
      constructor(){sources.push(this);queueMicrotask(()=>this.onmessage?.({data:JSON.stringify({
        event:"snapshot",revision:0,bridge_epoch:"epoch",active:{"voice-id-1":"history"},
        replies:[{reply_id:"history",state:"ready",media:[{url:"/old.mp4",media_seq:0}]}]
      })}));}
      close(){this.closed=true;}
    }
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(__dirname+"/app.js","utf8"),sandbox);
  const emit=(reply,revision,event="media_ready")=>sources.at(-1).onmessage({
    data:JSON.stringify({event,revision,reply:{session_id:"voice-id-1",bridge_epoch:"epoch",...reply}})
  });
  async function start(){await get("start").onclick();}
  async function send(text){get("text").value=text;await get("message").onsubmit({preventDefault(){}});return requests.filter(r=>r.url.endsWith("/prompt")).at(-1).body.reply_id;}
  return {get,requests,plays,sources,events,sandbox,emit,start,send};
}

test("offline during delayed health never restarts capture",async()=>{
 const b=browser();const original=b.sandbox.fetch;let release;
 b.sandbox.fetch=(url,options)=>url.endsWith("/health")?new Promise(resolve=>{release=async()=>resolve(await original(url,options));}):original(url,options);
 const pending=b.start();b.events.offline();await release();await pending;
 assert.equal(b.sources.length,0);
 assert.equal(b.get("text").disabled,true);
 assert.equal(b.get("start").disabled,false);
});
test("end while snapshot is pending settles start and ignores late snapshot",async()=>{
 const b=browser();
 b.sandbox.EventSource=class{constructor(){b.sources.push(this);}close(){this.closed=true;}};
 const pending=b.start();await Promise.resolve();await Promise.resolve();await Promise.resolve();
 while (!b.sources.length) await Promise.resolve();
 b.get("end").onclick();await pending;
 b.sources[0].onmessage({data:JSON.stringify({event:"snapshot",revision:0,bridge_epoch:"epoch",active:{}})});
 assert.equal(b.get("text").disabled,true);
 assert.equal(b.sources[0].closed,true);
});
test("start never speaks snapshots or submits a historical prompt",async()=>{
 const b=browser();await b.start();
 assert.equal(b.plays.length,0);
 assert.equal(b.requests.some(r=>r.url.endsWith("/prompt")),false);
 assert.equal(b.get("text").disabled,false); // microphone denied still permits typing
});
test("typed prompt uses snapshot CAS and duplicate media plays only once",async()=>{
 const b=browser();await b.start();const id=await b.send("你好");
 const prompt=b.requests.find(r=>r.url.endsWith("/prompt")).body;
 assert.equal(prompt.replaces_reply_id,"history");
 const reply={reply_id:id,state:"ready",text:"回复",media:[
   {media_seq:0,url:"/part0.mp4"},{media_seq:1,url:"/part1.mp4"}]};
 b.emit(reply,1);b.emit(reply,1);
 await Promise.resolve();
 assert.deepEqual(b.plays,["/part0.mp4"]);
 b.get("video-a").listeners.ended();
 await Promise.resolve();
 assert.deepEqual(b.plays,["/part0.mp4","/part1.mp4"]);
 b.get("video-b").listeners.ended();
 assert.equal(b.get("status").textContent,"这一轮已播放完");
 b.emit(reply,2);
 assert.equal(b.plays.length,2);
});
test("end clears both players and ignores late media",async()=>{
 const b=browser();await b.start();const id=await b.send("你好");
 b.emit({reply_id:id,state:"draining",media:[{media_seq:0,url:"/part0.mp4"}]},1);
 b.get("end").onclick();await Promise.resolve();await Promise.resolve();
 b.emit({reply_id:id,state:"ready",media:[{media_seq:0,url:"/part0.mp4"},{media_seq:1,url:"/late.mp4"}]},2);
 assert.equal(b.get("video-a").getAttribute("src"),null);
 assert.equal(b.get("video-b").getAttribute("src"),null);
 assert.equal(b.get("text").disabled,true);
 assert.equal(b.plays.includes("/late.mp4"),false);
});
test("late old play resolution cannot pause the next reply",async()=>{
 const b=browser();await b.start();const first=await b.send("第一轮");
 const video=b.get("video-a");const normal=video.play.bind(video);let resolveOld;
 video.play=()=>{video.paused=false;return new Promise(resolve=>{resolveOld=resolve;});};
 b.emit({reply_id:first,state:"ready",media:[{media_seq:0,url:"/old.mp4"}]},1);
 const second=await b.send("第二轮");video.play=normal;
 b.emit({reply_id:second,state:"ready",media:[{media_seq:0,url:"/new.mp4"}]},2);
 resolveOld();await Promise.resolve();await Promise.resolve();
 assert.equal(video.paused,false);
 assert.equal(video.getAttribute("src"),"/new.mp4");
});
test("background requires another user gesture and sends no new prompt",async()=>{
 const b=browser();await b.start();await b.send("你好");
 const count=b.requests.filter(r=>r.url.endsWith("/prompt")).length;
 b.sandbox.document.hidden=true;b.events.visibilitychange();
 b.sandbox.document.hidden=false;b.events.visibilitychange();
 await Promise.resolve();
 assert.equal(b.get("start").disabled,false);
 assert.equal(b.get("text").disabled,true);
 assert.equal(b.requests.filter(r=>r.url.endsWith("/prompt")).length,count);
});
