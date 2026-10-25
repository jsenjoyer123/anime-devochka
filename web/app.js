import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";
import { applyOutfit, removeOutfit } from "./outfit.js";

// Если своей модели нет в web/models/aiko.vrm — берём пример из репозитория three-vrm
const MODEL_URLS = [
  "models/aiko.vrm",
  "https://pixiv.github.io/three-vrm/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm",
];

// --- бережём память и батарейку ---
const MAX_FPS = 30;
const MAX_PIXEL_RATIO = 1.25;
const MAX_CHAT_MESSAGES = 150; // старые сообщения удаляем из DOM

const EMOTIONS = ["happy", "angry", "sad", "relaxed", "surprised"];
const EMOTION_WEIGHT = { happy: 0.8, angry: 0.7, sad: 0.7, relaxed: 0.6, surprised: 0.6 };
const TAG_RE = /\[(happy|angry|sad|relaxed|surprised|neutral)\]/gi;

const $ = (id) => document.getElementById(id);
const stage = $("stage");
const canvas = $("scene");
const chat = $("chat");
const input = $("input");
const sendBtn = $("send");
const learnBtn = $("learn");
const muteBtn = $("mute");
const micBtn = $("mic");
const subtitles = $("subtitles");
const thinkingEl = $("thinking");

// ============================================================
// 3D-сцена
// ============================================================

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "low-power" });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, MAX_PIXEL_RATIO));
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 20);
camera.position.set(0, 1.3, 1.5);

const light = new THREE.DirectionalLight(0xffffff, Math.PI * 0.9);
light.position.set(1, 2, 2);
scene.add(light, new THREE.AmbientLight(0xffffff, 0.6));

// Точка, куда она смотрит: обычно на зрителя, во время размышлений — вверх-в-сторону
const lookTarget = new THREE.Object3D();
scene.add(lookTarget);

function resize() {
  const { clientWidth: w, clientHeight: h } = stage;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(stage);
resize();

const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));

let vrm = null;
let outfit = null;
let cameraTarget = new THREE.Vector3(0, 1.2, 0);

// модель кладём в «подставку», которую можно крутить при переодевании
const holder = new THREE.Group();
scene.add(holder);

function disposeVRM() {
  if (!vrm) return;
  removeOutfit(vrm);
  holder.remove(vrm.scene);
  VRMUtils.deepDispose(vrm.scene);
  vrm = null;
}

async function loadVRM(url) {
  const gltf = await loader.loadAsync(url);
  const next = gltf.userData.vrm;
  if (!next) throw new Error("в файле нет VRM-данных");

  // оптимизации из three-vrm: меньше вершин и костей → меньше памяти
  VRMUtils.removeUnnecessaryVertices(gltf.scene);
  if (VRMUtils.combineSkeletons) VRMUtils.combineSkeletons(gltf.scene);
  else if (VRMUtils.removeUnnecessaryJoints) VRMUtils.removeUnnecessaryJoints(gltf.scene);
  VRMUtils.rotateVRM0(next);
  next.scene.traverse((o) => { o.frustumCulled = false; });

  disposeVRM();
  vrm = next;
  holder.rotation.y = 0;
  state.spin = -1;
  holder.add(vrm.scene);
  if (vrm.lookAt) vrm.lookAt.target = lookTarget;

  restPose();
  vrm.update(0);
  vrm.scene.updateMatrixWorld(true);
  if (outfit) applyOutfit(vrm, outfit);

  // кадрируем по голове: видно верхнюю половину тела, как у витуберов
  const head = vrm.humanoid.getNormalizedBoneNode("head");
  const headPos = head ? head.getWorldPosition(new THREE.Vector3()) : new THREE.Vector3(0, 1.4, 0);
  cameraTarget = new THREE.Vector3(0, headPos.y - 0.12, 0);
  camera.position.set(0, headPos.y - 0.04, 1.8);
  camera.lookAt(cameraTarget);
}

async function loadDefaultModel() {
  for (const url of MODEL_URLS) {
    try {
      await loadVRM(url);
      $("loading").hidden = true;
      return;
    } catch (e) {
      console.warn("Не удалось загрузить", url, e);
    }
  }
  $("loading").textContent = "Не получилось загрузить модель (・_・;) Перетащи сюда свой .vrm";
}

// перетаскивание своей модели
stage.addEventListener("dragover", (e) => { e.preventDefault(); stage.classList.add("dragover"); });
stage.addEventListener("dragleave", () => stage.classList.remove("dragover"));
stage.addEventListener("drop", async (e) => {
  e.preventDefault();
  stage.classList.remove("dragover");
  const file = e.dataTransfer.files[0];
  if (!file || !file.name.toLowerCase().endsWith(".vrm")) return;
  const url = URL.createObjectURL(file);
  try {
    await loadVRM(url);
    $("loading").hidden = true;
  } catch (err) {
    addMessage("error", `Не смогла надеть эту модель: ${err.message}`);
  } finally {
    URL.revokeObjectURL(url);
  }
});

// ============================================================
// Анимация: поза, дыхание, моргание, эмоции, губы
// ============================================================

const state = {
  emotion: "neutral",
  talking: false,
  thinking: false,
  mouth: 0,
  weights: Object.fromEntries(EMOTIONS.map((e) => [e, 0])),
  nextBlink: 2,
  blinkT: -1,
  spin: -1, // прогресс кружения при переодевании, -1 — не кружится
  spinApplied: false,
};

/** Переодеться: с кружением, новый образ надевается, когда она стоит спиной. */
function changeOutfit(next, animated = true) {
  outfit = next;
  $("outfit-name").textContent = next ? `· ${next.name}` : "";
  if (!vrm) return; // наденем сразу после загрузки модели
  if (!animated) {
    applyOutfit(vrm, outfit);
    return;
  }
  state.spin = 0;
  state.spinApplied = false;
}
window.aiko = { changeOutfit }; // поиграться из консоли браузера

function updateSpin(dt) {
  if (state.spin < 0) return;
  state.spin = Math.min(1, state.spin + dt / 1.2);
  const k = state.spin;
  const eased = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
  if (!state.spinApplied && k >= 0.5) {
    holder.rotation.y = 0; // аксессуары считаются в позе покоя
    applyOutfit(vrm, outfit);
    state.spinApplied = true;
  }
  holder.rotation.y = eased * Math.PI * 2;
  if (k >= 1) {
    holder.rotation.y = 0;
    state.spin = -1;
  }
}

function bone(name) {
  return vrm?.humanoid.getNormalizedBoneNode(name);
}

function restPose() {
  // из T-позы опускаем руки вдоль тела
  bone("leftUpperArm")?.rotation.set(0, 0, -1.2);
  bone("rightUpperArm")?.rotation.set(0, 0, 1.2);
  bone("leftLowerArm")?.rotation.set(0, -0.2, 0);
  bone("rightLowerArm")?.rotation.set(0, 0.2, 0);
}

const damp = (cur, target, k, dt) => cur + (target - cur) * (1 - Math.exp(-k * dt));

function animate(t, dt) {
  if (!vrm) return;
  const em = vrm.expressionManager;
  updateSpin(dt);

  // дыхание и покачивание
  const chest = bone("chest") || bone("spine");
  if (chest) chest.rotation.x = Math.sin(t * 1.6) * 0.015;
  const spine = bone("spine");
  if (spine) spine.rotation.z = Math.sin(t * 0.7) * 0.02;
  const neck = bone("neck");
  if (neck) {
    const tilt = state.thinking ? 0.18 : 0;
    neck.rotation.z = damp(neck.rotation.z, tilt + Math.sin(t * 0.9) * 0.04, 4, dt);
    neck.rotation.y = Math.sin(t * 0.5) * 0.05 + (state.talking ? Math.sin(t * 2.3) * 0.04 : 0);
    neck.rotation.x = state.talking ? Math.sin(t * 3.1) * 0.03 : 0;
  }
  bone("leftUpperArm")?.rotation.set(0, 0, -1.2 + Math.sin(t * 1.6) * 0.02);
  bone("rightUpperArm")?.rotation.set(0, 0, 1.2 - Math.sin(t * 1.6) * 0.02);

  // куда смотрит
  const look = state.thinking
    ? new THREE.Vector3(0.6, camera.position.y + 0.5, camera.position.z)
    : camera.position.clone().add(new THREE.Vector3(Math.sin(t * 0.4) * 0.1, 0, 0));
  lookTarget.position.lerp(look, 1 - Math.exp(-3 * dt));

  if (!em) return;

  // эмоции плавно перетекают друг в друга
  for (const e of EMOTIONS) {
    const target = state.emotion === e ? EMOTION_WEIGHT[e] : 0;
    state.weights[e] = damp(state.weights[e], target, 6, dt);
    em.setValue(e, state.weights[e]);
  }

  // моргание (при улыбке глаза и так прищурены)
  if (state.blinkT < 0 && t > state.nextBlink) state.blinkT = 0;
  let blink = 0;
  if (state.blinkT >= 0) {
    state.blinkT += dt;
    blink = Math.sin(Math.min(state.blinkT / 0.16, 1) * Math.PI);
    if (state.blinkT > 0.16) {
      state.blinkT = -1;
      state.nextBlink = t + 2 + Math.random() * 4;
    }
  }
  em.setValue("blink", state.weights.happy > 0.4 ? 0 : blink);

  // губы: по громкости голоса, а если его не слышно — имитация речи
  const level = voiceLevel();
  const mouthTarget = level !== null
    ? level
    : state.talking
      ? 0.15 + 0.6 * Math.abs(Math.sin(t * 13)) * (0.6 + 0.4 * Math.sin(t * 4.7))
      : 0;
  state.mouth = damp(state.mouth, mouthTarget, 18, dt);
  em.setValue("aa", state.mouth);
  em.setValue("oh", state.talking ? state.mouth * 0.3 * (1 + Math.sin(t * 7.1)) : 0);
}

// рендер-цикл с ограничением FPS; во скрытой вкладке браузер сам его останавливает
const clock = new THREE.Clock();
let acc = 0;
function loop() {
  requestAnimationFrame(loop);
  acc += clock.getDelta();
  if (acc < 1 / MAX_FPS) return;
  const dt = Math.min(acc, 0.1);
  acc = 0;
  animate(clock.elapsedTime, dt);
  vrm?.update(dt);
  renderer.render(scene, camera);
}
document.addEventListener("visibilitychange", () => { clock.getDelta(); acc = 0; });
loop();

// ============================================================
// Голос и субтитры
// ============================================================

let muted = localStorage.getItem("aiko-muted") === "1";
let serverTTS = false; // голос с сервера (edge-tts); иначе — браузерный синтез, если он есть
let voice = null;
let queue = [];
let speakToken = 0;

// один <audio> на всё время: новые не создаём, чтобы не копить память
const audioEl = new Audio();
let audioUrl = null;
let audioCtx = null;
let analyser = null;
let levelBuf = null;

// браузер разрешает звук только после первого действия пользователя
document.addEventListener("pointerdown", () => {
  audioCtx = new AudioContext();
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  levelBuf = new Uint8Array(analyser.fftSize);
  audioCtx.createMediaElementSource(audioEl).connect(analyser);
  analyser.connect(audioCtx.destination);
}, { once: true });

/** Громкость текущего звука 0..1 или null, если голос сейчас не играет. */
function voiceLevel() {
  if (!analyser || audioEl.paused) return null;
  analyser.getByteTimeDomainData(levelBuf);
  let sum = 0;
  for (const v of levelBuf) sum += ((v - 128) / 128) ** 2;
  return Math.min(1, Math.sqrt(sum / levelBuf.length) * 5);
}

function releaseAudio() {
  audioEl.pause();
  audioEl.onended = audioEl.onerror = null;
  if (audioUrl) URL.revokeObjectURL(audioUrl);
  audioUrl = null;
}

function fetchTTS(text) {
  return fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }).then((r) => (r.ok ? r.blob() : null)).catch(() => null);
}

function pickVoice() {
  const voices = speechSynthesis.getVoices().filter((v) => v.lang.toLowerCase().startsWith("ru"));
  // женские голоса, если есть
  voice = voices.find((v) => /milena|irina|anna|alena|svetlana|female|жен/i.test(v.name))
    || voices.find((v) => /google/i.test(v.name))
    || voices[0]
    || null;
}
if ("speechSynthesis" in window) {
  pickVoice();
  speechSynthesis.addEventListener("voiceschanged", pickVoice);
}

function updateMuteBtn() { muteBtn.textContent = muted ? "🔇" : "🔊"; }
updateMuteBtn();
muteBtn.onclick = () => {
  muted = !muted;
  localStorage.setItem("aiko-muted", muted ? "1" : "0");
  updateMuteBtn();
  if (muted) {
    releaseAudio();
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  }
};

function splitByEmotion(text) {
  const parts = [];
  let emotion = null;
  let last = 0;
  for (const m of text.matchAll(TAG_RE)) {
    if (m.index > last) parts.push({ emotion, text: text.slice(last, m.index) });
    emotion = m[1].toLowerCase();
    last = m.index + m[0].length;
  }
  parts.push({ emotion, text: text.slice(last) });
  return parts;
}

function forSpeech(text) {
  return text
    .replace(/`([^`]*)`/g, "$1")
    .replace(/[*_#>]/g, " ")
    .replace(/[^\p{L}\p{N}\s.,!?…:;()\-—«»"'/]/gu, "") // каомодзи и эмодзи не читаем вслух
    .replace(/\s+/g, " ")
    .trim();
}

/** Озвучивает ответ по предложениям, переключая эмоции по тегам. */
function speak(text) {
  stopSpeaking();
  for (const part of splitByEmotion(text)) {
    const sentences = part.text.match(/[^.!?…\n]+[.!?…]*/g) || [];
    let first = true;
    for (const s of sentences) {
      if (!s.trim()) continue;
      queue.push({ emotion: first ? part.emotion : null, text: s.trim() });
      first = false;
    }
  }
  playNext(++speakToken);
}

function stopSpeaking() {
  queue = [];
  speakToken++;
  releaseAudio();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  state.talking = false;
}

async function playNext(token) {
  if (token !== speakToken) return;
  const item = queue.shift();
  if (!item) {
    state.talking = false;
    setTimeout(() => { if (!state.talking) subtitles.textContent = ""; }, 2500);
    return;
  }
  if (item.emotion) state.emotion = item.emotion === "neutral" ? "neutral" : item.emotion;
  subtitles.textContent = item.text.replace(/`/g, "");
  state.talking = true;

  const spoken = forSpeech(item.text);
  // без голоса (или если синтез завис) — просто держим субтитр по длине текста
  const fallbackMs = Math.max(1200, item.text.length * 65);
  let done = false;
  const next = () => {
    if (done) return;
    done = true;
    playNext(token);
  };

  if (muted || !spoken) {
    setTimeout(next, fallbackMs);
    return;
  }

  if (serverTTS) {
    // следующую фразу синтезируем заранее, пока звучит текущая
    item.audio ??= fetchTTS(spoken);
    if (queue[0]) queue[0].audio ??= fetchTTS(forSpeech(queue[0].text));
    const blob = await item.audio;
    if (token !== speakToken) return;
    if (!blob) {
      setTimeout(next, fallbackMs);
      return;
    }
    releaseAudio();
    audioUrl = URL.createObjectURL(blob);
    audioEl.src = audioUrl;
    audioEl.onended = next;
    audioEl.onerror = () => setTimeout(next, 300);
    audioEl.play().catch(() => setTimeout(next, fallbackMs)); // звук ещё не разрешён — только субтитры
    return;
  }

  if (!("speechSynthesis" in window)) {
    setTimeout(next, fallbackMs);
    return;
  }
  const u = new SpeechSynthesisUtterance(spoken);
  u.lang = "ru-RU";
  if (voice) u.voice = voice;
  u.pitch = 1.45; // повыше — по-анимешному
  u.rate = 1.08;
  u.onend = next;
  u.onerror = () => setTimeout(next, fallbackMs);
  speechSynthesis.speak(u);
  setTimeout(next, fallbackMs * 3 + 4000); // страховка от зависшего onend в Chromium
}

// ============================================================
// Чат с бэкендом
// ============================================================

function addMessage(kind, text) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  // `код` показываем моноширинным, всё остальное — как текст (без innerHTML)
  text.split(/`([^`]+)`/).forEach((chunk, i) => {
    if (i % 2) {
      const code = document.createElement("code");
      code.textContent = chunk;
      div.append(code);
    } else {
      div.append(chunk);
    }
  });
  chat.append(div);
  while (chat.children.length > MAX_CHAT_MESSAGES) chat.firstElementChild.remove();
  chat.scrollTop = chat.scrollHeight;
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  learnBtn.disabled = busy;
  state.thinking = busy;
  if (busy) {
    state.emotion = "relaxed";
    thinkingEl.textContent = "хмм…";
    thinkingEl.hidden = false;
  } else {
    thinkingEl.hidden = true;
  }
}

async function ask(payload) {
  setBusy(true);
  stopSpeaking();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    // NDJSON: по событию на строку
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) handleEvent(JSON.parse(line));
      }
    }
  } catch (e) {
    addMessage("error", `Ошибка: ${e.message}`);
    state.emotion = "sad";
  } finally {
    setBusy(false);
  }
}

function handleEvent(ev) {
  if (ev.type === "tool") {
    thinkingEl.textContent = ev.text;
    addMessage("tool", ev.text);
  } else if (ev.type === "outfit") {
    if (ev.outfit) changeOutfit(ev.outfit);
  } else if (ev.type === "thought") {
    addMessage("tool", ev.text.replace(TAG_RE, "").trim());
  } else if (ev.type === "reply") {
    setBusy(false);
    addMessage("aiko", ev.text.replace(TAG_RE, "").trim());
    speak(ev.text);
    refreshStatus();
  } else if (ev.type === "error") {
    addMessage("error", ev.text);
    state.emotion = "sad";
  }
}

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || sendBtn.disabled) return;
  input.value = "";
  addMessage("user", text);
  ask({ message: text });
});

learnBtn.onclick = () => {
  addMessage("user", "Айко, изучи, пожалуйста, проект ✨");
  ask({ action: "learn" });
};

// голосовой ввод — только если браузер умеет (Chrome отправляет звук в Google)
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (Recognition) {
  micBtn.hidden = false;
  let rec = null;
  micBtn.onclick = () => {
    if (rec) { rec.stop(); return; }
    rec = new Recognition();
    rec.lang = "ru-RU";
    rec.interimResults = false;
    rec.onresult = (e) => {
      input.value = e.results[0][0].transcript;
      $("form").requestSubmit();
    };
    rec.onerror = (e) => addMessage("error", `Микрофон: ${e.error}`);
    rec.onend = () => { rec = null; micBtn.classList.remove("on"); };
    micBtn.classList.add("on");
    stopSpeaking();
    rec.start();
  };
}

let greeted = false;
async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    $("meta").textContent = `${s.project} · ${s.model}`;
    serverTTS = s.tts;
    if (s.outfit && !outfit) changeOutfit(s.outfit, false);
    learnBtn.hidden = false;
    learnBtn.textContent = s.has_notes ? "🔄 Переизучить проект" : "✨ Изучить проект";
    if (!greeted) {
      greeted = true;
      const hello = s.has_notes
        ? `[happy] С возвращением, семпай! Я помню проект ${s.project} по своим заметкам. Спрашивай что угодно!`
        : `[happy] Привет, семпай! Я Айко, твоя новая коллега. [surprised] Проект ${s.project} я ещё не видела — нажми «Изучить проект», и я всё-всё разберу!`;
      addMessage("aiko", hello.replace(TAG_RE, "").trim());
      // до первого клика браузер не даст ей заговорить — ждём его
      if (audioCtx) {
        speak(hello);
      } else {
        subtitles.textContent = "👆 кликни, чтобы услышать Айко";
        document.addEventListener("pointerdown", () => speak(hello), { once: true });
      }
    }
  } catch {
    $("meta").textContent = "сервер не отвечает";
  }
}

loadDefaultModel();
refreshStatus();
