// Microphone -> websocket -> speaker, plus a text fallback.
//
// Half-duplex by default: while the agent is speaking, mic frames are dropped.
// That is what stops the agent from hearing its own voice through your speakers
// and answering itself forever. Tick "let me interrupt" if you are on headphones.

const callBtn = document.getElementById("call");
const statusEl = document.getElementById("status");
const logEl = document.getElementById("log");
const form = document.getElementById("type-form");
const input = document.getElementById("type-input");
const talkOver = document.getElementById("talk-over");

let ws = null;
let micCtx = null;
let playCtx = null;
let stream = null;
let worklet = null;
let scheduleAt = 0;
let live = false;
const playing = new Set();

let lastRole = null;
let lastBody = null;

function setStatus(text) {
  statusEl.textContent = text;
}

function addLine(role, text, className = "") {
  const line = document.createElement("div");
  line.className = `line ${className}`.trim();

  const who = document.createElement("span");
  who.className = "who";
  who.textContent = role;

  const body = document.createElement("span");
  body.textContent = text;

  line.append(who, body);
  logEl.append(line);
  logEl.scrollTop = logEl.scrollHeight;
  return body;
}

function addFragment(role, text) {
  if (lastRole !== role || !lastBody) {
    lastBody = addLine(role, "");
    lastRole = role;
  }
  lastBody.textContent += text;
  logEl.scrollTop = logEl.scrollHeight;
}

function agentSpeaking() {
  // If playback is not actually running, no sound is reaching the mic, so the
  // mic must stay open. A suspended context would otherwise freeze currentTime
  // and mute the caller forever.
  if (!playCtx || playCtx.state !== "running") return false;
  // Nothing queued and the schedule cursor has passed: the speakers are quiet.
  return playing.size > 0 || playCtx.currentTime < scheduleAt + 0.25;
}

function playPCM(buffer) {
  if (!playCtx) return;

  const pcm = new Int16Array(buffer);
  if (pcm.length === 0) return;

  const floats = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;

  const audio = playCtx.createBuffer(1, floats.length, 24000);
  audio.copyToChannel(floats, 0);

  const source = playCtx.createBufferSource();
  source.buffer = audio;
  source.connect(playCtx.destination);

  const now = playCtx.currentTime;
  if (scheduleAt < now + 0.02) scheduleAt = now + 0.02;
  source.start(scheduleAt);
  scheduleAt += audio.duration;

  playing.add(source);
  source.onended = () => playing.delete(source);
}

function stopPlayback() {
  for (const source of playing) {
    try {
      source.stop();
    } catch {
      /* already stopped */
    }
  }
  playing.clear();
  scheduleAt = 0;
}

function onMessage(event) {
  if (typeof event.data !== "string") {
    playPCM(event.data);
    return;
  }

  const message = JSON.parse(event.data);

  switch (message.type) {
    case "ready":
      setStatus("connected");
      break;
    case "transcript":
      addFragment(message.role, message.text);
      break;
    case "interrupted":
      stopPlayback();
      break;
    case "tool":
      addLine("tool", `${message.name} → ${JSON.stringify(message.result).slice(0, 140)}`, "tool");
      break;
    case "turn_complete":
      lastRole = null;
      lastBody = null;
      setStatus("listening");
      break;
    case "ended":
      addLine("system", "call ended", "system");
      teardown();
      break;
    case "error":
      addLine("error", message.message, "error");
      setStatus("error");
      break;
  }
}

async function startPlayback() {
  playCtx = new AudioContext({ sampleRate: 24000 });
  await playCtx.resume();
}

async function startMic() {
  stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      // AGC lifts quiet speaker bleed back into the mic, which restarts the loop.
      autoGainControl: false,
    },
  });

  micCtx = new AudioContext({ sampleRate: 16000 });
  await micCtx.audioWorklet.addModule("/static/pcm-worklet.js");

  const source = micCtx.createMediaStreamSource(stream);
  worklet = new AudioWorkletNode(micCtx, "pcm-recorder");
  worklet.port.onmessage = (event) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (!talkOver.checked && agentSpeaking()) return;
    ws.send(event.data);
  };

  // Some browsers only run a worklet that reaches a destination. A muted gain
  // node keeps it running without echoing the mic back to the speakers.
  const silent = micCtx.createGain();
  silent.gain.value = 0;
  source.connect(worklet).connect(silent).connect(micCtx.destination);
  await micCtx.resume();
}

function stopAudio() {
  stopPlayback();

  if (worklet) {
    worklet.port.onmessage = null;
    worklet.disconnect();
    worklet = null;
  }
  if (stream) {
    stream.getTracks().forEach((track) => track.stop());
    stream = null;
  }
  if (micCtx) {
    micCtx.close().catch(() => {});
    micCtx = null;
  }
  if (playCtx) {
    playCtx.close().catch(() => {});
    playCtx = null;
  }
}

async function start() {
  callBtn.disabled = true;
  setStatus("connecting");
  lastRole = null;
  lastBody = null;

  try {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${scheme}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    ws.onmessage = onMessage;
    ws.onclose = () => {
      if (live) addLine("system", "call ended", "system");
      teardown();
    };

    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.addEventListener("error", reject, { once: true });
    });

    live = true;

    try {
      await startPlayback();
    } catch {
      addLine("error", "audio output unavailable — you can still type", "error");
    }

    // A missing or blocked microphone should not end the call: the text box works.
    try {
      await startMic();
    } catch (error) {
      addLine("error", `microphone unavailable (${error.name}) — type below instead`, "error");
    }

    if (!live) return;

    callBtn.textContent = "End call";
    callBtn.disabled = false;
  } catch (error) {
    addLine("error", error.message || String(error), "error");
    teardown();
  }
}

function teardown() {
  live = false;
  stopAudio();

  if (ws) {
    ws.onclose = null;
    if (ws.readyState <= WebSocket.OPEN) ws.close();
    ws = null;
  }

  callBtn.textContent = "Start call";
  callBtn.disabled = false;
  setStatus("idle");
}

callBtn.addEventListener("click", () => {
  if (live) {
    teardown();
  } else {
    start();
  }
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "text", text }));
  addLine("you", text);
  input.value = "";
});
