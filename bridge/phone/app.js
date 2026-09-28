const el = (id) => document.getElementById(id);
const status = (text) => { el("status").textContent = text; };
const uuid = () => crypto.randomUUID();
const session = sessionStorage.getItem("voice-session") || "voice-" + uuid();
sessionStorage.setItem("voice-session", session);
let epoch = "", previous = null, current = null, version = 0, started = false, source = null;
let media = [], played = new Set(), ready = false, playing = null, activeVideo = el("video-a");
let stream = null, context = null, worklet = null, socket = null, micGeneration = 0;
let frames = [], recording = null, sent = 0, received = 0, uploading = false, lastRevision = -1;
let assistantBubble = null, cancelPromise = Promise.resolve(), submission = 0;
let startGeneration = 0, rejectStart = null;
const otherVideo = () => activeVideo === el("video-a") ? el("video-b") : el("video-a");

async function api(path, body, options = {}) {
  const response = await fetch("/voice/api/" + path, {
    credentials: "same-origin", ...options,
    ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
  });
  const type = response.headers.get("content-type") || "";
  if (response.status === 401 || !type.includes("application/json")) throw new Error("登录已失效，请刷新页面重新登录");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.detail || "请求失败");
  return data;
}
function bubble(who, text) {
  const node = document.createElement("div"); node.className = "bubble " + who;
  const label = document.createElement("small"); label.textContent = who === "user" ? "我" : "她";
  const content = document.createElement("span"); content.textContent = text;
  node.append(label, content); el("transcript").append(node); node.scrollIntoView({ block: "nearest" });
  return content;
}
function clearPlayback() {
  playing = null; media = []; played.clear(); ready = false;
  for (const video of [el("video-a"), el("video-b")]) {
    video.pause(); video.removeAttribute("src"); video.load(); video.hidden = true;
  }
  el("empty").hidden = false; el("resume").hidden = true;
}
function cancelCurrent(reason) {
  const old = current, oldEpoch = epoch; current = null; version++; clearPlayback();
  if (!old) return cancelPromise;
  cancelPromise = cancelPromise.catch(() => {}).then(() =>
    api("replies/" + old + "/cancel", { bridge_epoch: oldEpoch, reason }, { keepalive: true }));
  return cancelPromise;
}
async function stop(reason, message) {
  started = false; submission++; startGeneration++;
  rejectStart?.(new Error("Start cancelled")); rejectStart = null;
  source?.close(); source = null; stopMic();
  el("start").disabled = false; el("end").disabled = true; el("mic").disabled = true;
  el("text").disabled = true; el("send").disabled = true;
  const task = cancelCurrent(reason);
  status(message);
  try { await task; } catch { /* Local capture/playback already stopped. */ }
}
function playbackUpdate() {
  if (!started || playing !== null || !current) return;
  const next = media.find((item) => !played.has(item.media_seq));
  if (!next) {
    status(ready ? "这一轮已播放完" : "正在生成视频…");
    return;
  }
  playing = next.media_seq;
  const hidden = otherVideo();
  if (hidden.getAttribute("src") === next.url) {
    activeVideo.hidden = true; activeVideo = hidden;
  } else activeVideo.src = next.url;
  activeVideo.hidden = false; el("empty").hidden = true;
  const guard = version;
  activeVideo.play().then(() => {
    if (guard !== version || !started) return;
    status("正在说话");
  }).catch(() => {
    if (guard === version) { el("resume").hidden = false; status("请点击视频上的按钮允许播放"); }
  });
  const following = media.find((item) => item.media_seq === next.media_seq + 1);
  const preload = otherVideo();
  if (following && preload.getAttribute("src") !== following.url) { preload.src = following.url; preload.load(); }
}
for (const video of [el("video-a"), el("video-b")]) {
  video.addEventListener("ended", () => {
    if (video !== activeVideo || playing === null || !started) return;
    played.add(playing); playing = null; playbackUpdate();
  });
  video.addEventListener("error", () => {
    if (video === activeVideo && current && video.getAttribute("src"))
      void stop("media_failed", "视频加载失败，请重新开始");
  });
}
el("resume").onclick = async () => {
  try { await activeVideo.play(); el("resume").hidden = true; status("正在说话"); }
  catch { status("浏览器仍未允许播放，请检查媒体权限"); }
};
function applyEvent(item) {
  if (item.revision <= lastRevision && item.event !== "snapshot") return;
  lastRevision = item.revision;
  if (item.event === "snapshot") {
    if (item.bridge_epoch !== epoch) { void stop("epoch_changed", "云端服务已重启，请重新开始"); return; }
    previous = item.active[session] ?? null;
    // Snapshots establish CAS state; never replay historical media.
    return;
  }
  const reply = item.reply;
  if (reply.session_id !== session || reply.bridge_epoch !== epoch) return;
  if (item.event === "begun") previous = reply.reply_id;
  if (reply.reply_id !== current) return;
  if (assistantBubble) assistantBubble.textContent = reply.text || "…";
  if (reply.state === "failed" || reply.state === "cancelled") {
    clearPlayback(); current = null; version++;
    status(reply.state === "failed" ? "本轮生成失败，可发送新的消息" : "已停止本轮回复");
    return;
  }
  media = reply.media;
  ready = reply.state === "ready";
  if (playing !== null) {
    const next = media.find((part) => part.media_seq === playing + 1);
    if (next && otherVideo().getAttribute("src") !== next.url) { otherVideo().src = next.url; otherVideo().load(); }
  }
  playbackUpdate();
}
async function submit(text) {
  if (!started || !text.trim()) return;
  const ticket = ++submission;
  await cancelCurrent("new_turn");
  if (!started || ticket !== submission) return;
  const guard = version, reply = uuid();
  current = reply; ready = false; media = []; played.clear();
  bubble("user", text); assistantBubble = bubble("assistant", "…"); status("正在回复…");
  try {
    const result = await api("prompt", { reply_id: reply, session_id: session, bridge_epoch: epoch,
      replaces_reply_id: previous, text });
    if (guard !== version) return;
    previous = result.reply_id;
  } catch (error) {
    if (guard === version) { clearPlayback(); current = null; status(String(error.message)); }
  }
}
el("message").onsubmit = async (event) => {
  event.preventDefault(); const text = el("text").value.trim(); if (!text) return;
  el("text").value = ""; el("send").disabled = true;
  try { await submit(text); } finally { el("send").disabled = !started; }
};
function stopMic() {
  micGeneration++; recording = null; frames = []; uploading = false;
  if (socket) { socket.onclose = null; socket.close(); socket = null; }
  worklet?.disconnect(); worklet = null;
  stream?.getTracks().forEach((track) => track.stop()); stream = null;
  void context?.close(); context = null;
  el("mic").textContent = "麦克风关"; el("mic").setAttribute("aria-pressed", "false");
}
async function uploadCapture(samples, guard) {
  if (!samples.length || uploading) return;
  uploading = true;
  const pcm = new Int16Array(samples.length * 512);
  samples.forEach((frame, index) => pcm.set(frame, index * 512));
  const capture = uuid();
  try {
    const response = await fetch("/voice/api/stt?capture_id=" + capture, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/octet-stream" }, body: pcm.buffer
    });
    if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error("语音识别失败或登录过期");
    const result = await response.json();
    if (guard === micGeneration && started && result.capture_id === capture && result.text)
      await submit(result.text);
  } catch (error) {
    if (guard === micGeneration) status(error.message);
  } finally { if (guard === micGeneration) uploading = false; }
}
async function startMic() {
  if (stream) return;
  const guard = ++micGeneration;
  const acquired = await navigator.mediaDevices.getUserMedia({ audio: {
    channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
  } });
  if (!started || guard !== micGeneration) { acquired.getTracks().forEach((t) => t.stop()); return; }
  stream = acquired;
  const audioContext = new AudioContext(); context = audioContext;
  try {
    await audioContext.resume();
    if (!started || guard !== micGeneration) return;
    await audioContext.audioWorklet.addModule("/voice/static/capture-worklet.js");
  } catch (error) {
    if (guard !== micGeneration) return;
    throw error;
  }
  if (!started || guard !== micGeneration) return;
  worklet = new AudioWorkletNode(audioContext, "pcm-capture");
  audioContext.createMediaStreamSource(acquired).connect(worklet);
  const silent = audioContext.createGain(); silent.gain.value = 0;
  worklet.connect(silent).connect(audioContext.destination);
  socket = new WebSocket(location.origin.replace(/^http/, "ws") + "/voice/api/vad");
  socket.binaryType = "arraybuffer"; sent = received = 0; frames = []; recording = null;
  socket.onclose = () => { if (guard === micGeneration && started) void stop("vad_disconnected", "收音连接中断，请重新开始"); };
  socket.onmessage = (event) => {
    if (guard !== micGeneration || !started) return;
    const result = JSON.parse(event.data);
    if (result.sequence !== received) { void stop("vad_sequence", "收音状态失去同步，请重新开始"); return; }
    received++;
    if (result.speech && recording === null && !uploading) {
      recording = frames.filter((entry) => entry.sequence >= result.sequence - 16).map((entry) => entry.data);
      if (el("mode").value === "barge" && current) void cancelCurrent("barge_in").catch(() => stop("cancel_failed", "取消失败，请重新开始"));
      status("正在听…");
    }
    if (result.ended && recording) {
      const captured = recording; recording = null; frames = [];
      void uploadCapture(captured, guard);
    }
  };
  worklet.port.onmessage = (event) => {
    if (guard !== micGeneration || !started || socket?.readyState !== WebSocket.OPEN) return;
    if (el("mode").value === "queue" && playing !== null) {
      recording = null; frames = [];
      // Send silence through VAD to reset endpointing without capturing player audio.
      if (sent - received < 32) { socket.send(new Int16Array(512)); sent++; }
      return;
    }
    if (uploading) return;
    if (sent - received > 64 || socket.bufferedAmount > 65536) {
      void stop("capture_backpressure", "收音处理积压，请重新开始"); return;
    }
    const data = new Int16Array(event.data);
    frames.push({ data, sequence: sent }); if (frames.length > 80) frames.shift();
    if (recording) recording.push(data);
    socket.send(data); sent++;
    if (recording && recording.length >= 937) {
      const captured = recording; recording = null; frames = [];
      void uploadCapture(captured, guard);
    }
  };
  acquired.getTracks()[0].onended = () => { if (guard === micGeneration) { stopMic(); status("麦克风权限或设备已断开"); } };
  el("mic").textContent = "麦克风开"; el("mic").setAttribute("aria-pressed", "true");
}
el("mic").onclick = () => {
  if (stream) { stopMic(); status("麦克风已关闭，视频可继续播放"); }
  else void startMic().catch((error) => { stopMic(); status("无法使用麦克风：" + error.message); });
};
el("start").onclick = async () => {
  const attempt = ++startGeneration;
  el("start").disabled = true;
  try {
    const health = await api("health");
    if (attempt !== startGeneration) return;
    if (health.status !== "ready") throw new Error(health.status === "loading" ? "云端模型正在加载，请稍后再点开始" : "云端初始化失败");
    if (document.hidden) throw new Error("页面已转到后台，请返回后重新开始");
    epoch = health.bridge_epoch; started = true; version++; lastRevision = -1; previous = null;
    source = new EventSource("/voice/api/events");
    await new Promise((resolve, reject) => {
      rejectStart = reject;
      source.onmessage = (event) => {
        if (attempt !== startGeneration) return;
        const item = JSON.parse(event.data); applyEvent(item);
        if (item.event === "snapshot") { rejectStart = null; resolve(); }
      };
      source.onerror = () => {
        if (attempt !== startGeneration) return;
        reject(new Error("对话连接中断")); void stop("network_lost", "连接中断，请重新开始");
      };
    });
    if (attempt !== startGeneration || !started) return;
    el("end").disabled = false; el("mic").disabled = false; el("text").disabled = false; el("send").disabled = false;
    try {
      await startMic();
      if (attempt === startGeneration && started) status("已开始，可以说话或发送文字");
    } catch {
      if (attempt === startGeneration && started) { stopMic(); status("麦克风未获授权，可以发送文字或重新开启麦克风"); }
    }
  } catch (error) {
    if (attempt === startGeneration) { started = false; el("start").disabled = false; status(error.message); }
  }
};
el("end").onclick = () => void stop("ended", "对话已结束");
window.addEventListener("offline", () => void stop("network_lost", "网络已断开，请重新开始"));
document.addEventListener("visibilitychange", () => { if (document.hidden && started) void stop("backgrounded", "已暂停，点击开始继续"); });
window.addEventListener("pagehide", () => { if (started) void stop("page_hidden", "已停止"); });
