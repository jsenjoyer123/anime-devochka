import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";
import { applyOutfit, removeOutfit } from "./outfit.js";
import { Motion, idlePose, GESTURE_NAMES } from "./motion.js";
import { showCode, showDiff, showDiagram, isScreenVisible } from "./screen.js";
import { Ear } from "./mic.js";

// Если своей модели нет в web/models/aiko.vrm — берём пример из репозитория three-vrm
const MODEL_URLS = [
  "models/aiko.vrm",
  "https://pixiv.github.io/three-vrm/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm",
];

// --- бережём память и батарейку ---
const MAX_FPS = 30;
const SLEEP_FPS = 10;
const MAX_PIXEL_RATIO = 1.25;
const MAX_CHAT_MESSAGES = 150; // старые сообщения удаляем из DOM

const EMOTIONS = ["happy", "angry", "sad", "relaxed", "surprised"];
const EMOTION_WEIGHT = { happy: 0.8, angry: 0.7, sad: 0.7, relaxed: 0.6, surprised: 0.6 };
// теги в ответе модели: эмоции ([happy]) и жесты ([wave]) — не озвучиваются и не показываются
const TAG_RE = new RegExp(`\\[(happy|angry|sad|relaxed|surprised|neutral|${GESTURE_NAMES.join("|")})\\]`, "gi");
const isGesture = (tag) => GESTURE_NAMES.includes(tag);

const $ = (id) => document.getElementById(id);

// ?obs=1 — окно для OBS: прозрачный фон, без чата, повторяет основную вкладку
// ?embed=vscode — внутри боковой панели VS Code
const params = new URLSearchParams(location.search);
const OBS = params.has("obs");
const EMBED = params.get("embed");
if (OBS) document.documentElement.classList.add("obs");
if (EMBED) document.body.classList.add("embed");

// основная вкладка пересылает зрителям (OBS) всё, что видно на сцене; если зрителей нет — молчит
let viewers = 0;
let viewersCheckedAt = -Infinity;
function mirror(event) {
  if (OBS) return;
  const now = performance.now();
  if (!viewers && now - viewersCheckedAt < 15000) return;
  viewersCheckedAt = now;
  fetch("/api/broadcast", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event }),
  }).then((r) => r.json()).then((d) => { viewers = d.viewers; }).catch(() => {});
}
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

  motion.reset(idlePose(0, state));
  motion.update(0, idlePose(0, state), bone);
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

let projectModelUrl = null; // своя модель для проекта: web/models/projects/<проект>.vrm

async function loadDefaultModel() {
  for (const url of [projectModelUrl, ...MODEL_URLS].filter(Boolean)) {
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
  visemes: { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 },
  weights: Object.fromEntries(EMOTIONS.map((e) => [e, 0])),
  nextBlink: 2,
  blinkT: -1,
  spin: -1, // прогресс кружения при переодевании, -1 — не кружится
  spinApplied: false,
  sleeping: false,
  tilt: 0,          // наклон головы от реакций
  closedEyes: 0,    // 0..1, прикрытые глаза (сон, «погладили»)
  gaze: null,       // { pos, until } — куда она сейчас глянула сама
  pointer: null,    // { pos, until } — курсор семпая, за которым следят глаза
  eyesClosedUntil: 0,
};

const motion = new Motion();

// настроение: копится от поглаживаний, тычков и слов семпая, медленно возвращается к нулю
let mood = Number(localStorage.getItem("aiko-mood")) || 0;
function shiftMood(delta) {
  mood = Math.max(-1, Math.min(1, mood + delta));
  localStorage.setItem("aiko-mood", mood.toFixed(2));
  $("mood").textContent = mood > 0.5 ? "🥰" : mood > 0.15 ? "😊" : mood < -0.5 ? "😤" : mood < -0.15 ? "😒" : "😐";
}
const baseEmotion = () => (mood > 0.4 ? "relaxed" : mood < -0.4 ? "sad" : "neutral");

/** Переодеться: с кружением, новый образ надевается, когда она стоит спиной. */
function changeOutfit(next, animated = true) {
  outfit = next;
  mirror({ t: "outfit", outfit: next, animated });
  $("outfit-name").textContent = next ? `· ${next.name}` : "";
  stage.dataset.bg = next?.background || "sakura";
  if (!vrm) return; // наденем сразу после загрузки модели
  if (!animated) {
    applyOutfit(vrm, outfit);
    return;
  }
  state.spin = 0;
  state.spinApplied = false;
}
window.aiko = { changeOutfit, play: (g) => motion.play(g), get gesture() { return motion.current?.name ?? null; } }; // поиграться из консоли браузера

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

const damp = (cur, target, k, dt) => cur + (target - cur) * (1 - Math.exp(-k * dt));

function animate(t, dt) {
  if (!vrm) return;
  const em = vrm.expressionManager;
  updateSpin(dt);

  // тело: поза покоя + жест
  motion.update(dt, idlePose(t, state), bone);
  holder.position.y = motion.hopOffset();
  // когда на экране код — отходит в сторону, чтобы не загораживать
  holder.position.x = damp(holder.position.x, isScreenVisible() ? 0.22 : 0, 3, dt);

  // куда смотрит: задумалась → вверх-в-сторону, иначе на курсор, на что-то своё или на зрителя
  const now = performance.now();
  let look;
  if (state.thinking) look = new THREE.Vector3(0.6, camera.position.y + 0.5, camera.position.z);
  else if (state.sleeping) look = new THREE.Vector3(0, cameraTarget.y - 0.6, camera.position.z);
  else if (state.gaze && state.gaze.until > now) look = state.gaze.pos;
  else if (state.pointer && state.pointer.until > now) look = state.pointer.pos;
  else look = camera.position.clone().add(new THREE.Vector3(Math.sin(t * 0.4) * 0.1, 0, 0));
  lookTarget.position.lerp(look, 1 - Math.exp(-4 * dt));

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
  state.closedEyes = damp(state.closedEyes, state.sleeping ? 1 : state.eyesClosedUntil > now ? 0.9 : 0, 5, dt);
  em.setValue("blink", Math.max(state.closedEyes, state.weights.happy > 0.4 ? 0 : blink));

  // губы: форма гласной по спектру голоса, а если его не слышно — имитация речи
  const shape = voiceShape() ?? (state.talking ? fakeShape(t) : null);
  for (const v of VISEMES) {
    state.visemes[v] = damp(state.visemes[v], shape ? shape[v] : 0, 20, dt);
    em.setValue(v, state.visemes[v]);
  }
}

const VISEMES = ["aa", "ih", "ou", "ee", "oh"];

/** Когда звука нет (браузерный синтез, окно OBS), рот двигается по правдоподобному узору. */
function fakeShape(t) {
  const open = 0.15 + 0.6 * Math.abs(Math.sin(t * 13)) * (0.6 + 0.4 * Math.sin(t * 4.7));
  const v = VISEMES[Math.floor(t * 7) % VISEMES.length];
  return Object.fromEntries(VISEMES.map((x) => [x, x === v ? open : x === "aa" ? open * 0.3 : 0]));
}

// рендер-цикл с ограничением FPS; во скрытой вкладке браузер сам его останавливает
const clock = new THREE.Clock();
let acc = 0;
function loop() {
  requestAnimationFrame(loop);
  acc += clock.getDelta();
  if (acc < 1 / (state.sleeping ? SLEEP_FPS : MAX_FPS)) return; // спящей хватит 10 FPS
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
let playing = false; // очередь речи сейчас проигрывается
let voiceEmotion = "neutral"; // с какой эмоцией звучат фразы в очереди: тег действует до следующего

// один <audio> на всё время: новые не создаём, чтобы не копить память
const audioEl = new Audio();
let audioUrl = null;
let audioCtx = null;
let analyser = null;
let levelBuf = null;
let freqBuf = null;

// браузер разрешает звук только после первого действия пользователя
document.addEventListener("pointerdown", () => {
  audioCtx = new AudioContext();
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  levelBuf = new Uint8Array(analyser.fftSize);
  freqBuf = new Uint8Array(analyser.frequencyBinCount);
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

/**
 * Форма рта по спектру: грубая оценка гласной по энергии в полосах формант.
 * Низкие частоты без высоких — «у/о», средние — «а», высокие — «и/э».
 */
function voiceShape() {
  const level = voiceLevel();
  if (level === null) return null;
  analyser.getByteFrequencyData(freqBuf);
  const hz = audioCtx.sampleRate / analyser.fftSize;
  const band = (from, to) => {
    let sum = 0;
    const a = Math.floor(from / hz), b = Math.ceil(to / hz);
    for (let i = a; i < b; i++) sum += freqBuf[i];
    return sum / Math.max(1, b - a) / 255;
  };
  const low = band(250, 700), mid = band(700, 1400), high = band(1800, 3500);
  const total = low + mid + high + 1e-6;
  const [l, m, h] = [low / total, mid / total, high / total];
  const open = Math.min(1, level * 1.2);
  return {
    aa: open * Math.min(1, m * 1.8),
    oh: open * Math.max(0, l - h) * 1.2,
    ou: open * Math.max(0, l - m) * 1.4,
    ee: open * Math.max(0, h - l) * 1.6,
    ih: open * Math.max(0, h - m) * 1.2,
  };
}

function releaseAudio() {
  audioEl.pause();
  audioEl.onended = audioEl.onerror = null;
  if (audioUrl) URL.revokeObjectURL(audioUrl);
  audioUrl = null;
}

function fetchTTS(text, emotion) {
  return fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, emotion }),
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

/** Режет ответ на куски по тегам: у куска своя эмоция и, возможно, жест в начале. */
function splitByTags(text) {
  const parts = [];
  let emotion = null;
  let gesture = null;
  let last = 0;
  const flush = (end) => {
    if (end > last || gesture) parts.push({ emotion, gesture, text: text.slice(last, end) });
    gesture = null;
  };
  for (const m of text.matchAll(TAG_RE)) {
    const tag = m[1].toLowerCase();
    if (m.index > last && text.slice(last, m.index).trim()) flush(m.index);
    if (isGesture(tag)) gesture = tag;
    else emotion = tag;
    last = m.index + m[0].length;
  }
  flush(text.length);
  return parts;
}

const stripTags = (text) => text.replace(TAG_RE, "").replace(/ {2,}/g, " ").trim();

function forSpeech(text) {
  return text
    .replace(/`([^`]*)`/g, "$1")
    .replace(/[*_#>]/g, " ")
    .replace(/[^\p{L}\p{N}\s.,!?…:;()\-—«»"'/]/gu, "") // каомодзи и эмодзи не читаем вслух
    .replace(/\s+/g, " ")
    .trim();
}

// браузерный синтез: поправки к pitch и rate по эмоции (у edge-tts свои — на сервере)
const BROWSER_VOICE = { happy: [0.1, 0.05], surprised: [0.2, 0.03], angry: [-0.05, 0.08], sad: [-0.25, -0.12], relaxed: [-0.1, -0.08] };

/** Озвучивает ответ по предложениям, переключая эмоции по тегам. */
function speak(text) {
  stopSpeaking();
  enqueueSpeech(text);
}

/** Добавить текст в очередь речи, не перебивая; если она молчит — начать говорить. */
function enqueueSpeech(text) {
  for (const part of splitByTags(text)) {
    if (part.emotion) voiceEmotion = part.emotion;
    // режем по концу предложения (знак + пробел), чтобы не рвать `server.py` и версии вроде 1.2
    const sentences = part.text.split(/(?<=[.!?…])\s+|\n+/).filter((s) => s.trim());
    if (!sentences.length && part.gesture) sentences.push(""); // жест без слов
    sentences.forEach((s, i) => {
      queue.push({
        emotion: i === 0 ? part.emotion : null,
        gesture: i === 0 ? part.gesture : null,
        voice: voiceEmotion,
        text: s.trim(),
      });
    });
  }
  if (!playing) playNext(speakToken);
}

/** Короткая реплика «между делом»: не перебивает, если она уже говорит. */
function quip(text) {
  if (state.talking || playing || muted) return;
  speak(text);
}

function stopSpeaking() {
  queue = [];
  voiceEmotion = "neutral";
  speakToken++;
  playing = false;
  releaseAudio();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  state.talking = false;
}

async function playNext(token) {
  if (token !== speakToken) return;
  const item = queue.shift();
  playing = Boolean(item);
  if (!item) {
    state.talking = false;
    mirror({ t: "quiet" });
    setTimeout(() => {
      if (state.talking) return;
      subtitles.textContent = "";
      if (!state.thinking && !state.sleeping) state.emotion = baseEmotion();
    }, 2500);
    return;
  }
  if (item.emotion) state.emotion = item.emotion;
  if (item.gesture) motion.play(item.gesture);
  mirror({ t: "say", emotion: item.emotion, gesture: item.gesture, text: item.text });
  if (!item.text) {
    setTimeout(() => playNext(token), 600);
    return;
  }
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
    item.audio ??= fetchTTS(spoken, item.voice);
    if (queue[0]) queue[0].audio ??= fetchTTS(forSpeech(queue[0].text), queue[0].voice);
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
  const [dp, dr] = BROWSER_VOICE[item.voice] || [0, 0];
  u.pitch = 1.45 + dp; // повыше — по-анимешному
  u.rate = 1.08 + dr;
  u.onend = next;
  u.onerror = () => setTimeout(next, fallbackMs);
  speechSynthesis.speak(u);
  setTimeout(next, fallbackMs * 3 + 4000); // страховка от зависшего onend в Chromium
}

// ============================================================
// Чат с бэкендом
// ============================================================

/** `код` показываем моноширинным, всё остальное — как текст (без innerHTML). */
function renderText(div, text) {
  div.replaceChildren();
  text.split(/`([^`]+)`/).forEach((chunk, i) => {
    if (i % 2) {
      const code = document.createElement("code");
      code.textContent = chunk;
      div.append(code);
    } else {
      div.append(chunk);
    }
  });
}

function addMessage(kind, text) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  renderText(div, text);
  chat.append(div);
  while (chat.children.length > MAX_CHAT_MESSAGES) chat.firstElementChild.remove();
  chat.scrollTop = chat.scrollHeight;
  return div;
}

/** Айко просит разрешения запустить команду или закоммитить — решает только семпай. */
function askConfirm(ev) {
  const commit = ev.kind === "commit";
  const more = commit && ev.files.length > 5 ? ` и ещё ${ev.files.length - 5}` : "";
  const div = addMessage("aiko", commit
    ? `Закоммитить ${ev.add_all ? "все изменения" : "то, что в индексе"}: ${ev.files.slice(0, 5).map((f) => `\`${f}\``).join(", ")}${more}? Сообщение можно поправить:`
    : ev.kind === "undo"
      ? `Отменить мой коммит \`${ev.commit}\` «${ev.subject}»? Изменения вернутся в индекс, ничего не потеряется.`
      : `Можно запущу \`${ev.command}\`?${ev.reason ? `\n${ev.reason}` : ""}`);
  // сообщение коммита семпай правит прямо здесь — уйдёт то, что в поле
  const text = commit ? Object.assign(document.createElement("textarea"), { className: "commit-msg", value: ev.message, rows: 3 }) : null;
  const row = document.createElement("div");
  row.className = "ask";
  const answer = async (approve, always = false) => {
    const message = text?.value.trim();
    text?.setAttribute("readonly", "");
    row.replaceChildren(!approve ? "⛔ запрещено" : commit ? "✅ коммитим" : always ? "✅ разрешено навсегда (убрать — в меню 📁)" : "✅ разрешено");
    await fetch("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: ev.id, approve, always, ...(message ? { message } : {}) }),
    }).catch(() => {});
  };
  const yes = Object.assign(document.createElement("button"), { className: "yes", textContent: commit ? "Закоммитить" : "Разрешить" });
  const no = Object.assign(document.createElement("button"), { className: "no", textContent: commit ? "Не надо" : "Запретить" });
  yes.onclick = () => answer(true);
  no.onclick = () => answer(false);
  row.append(yes);
  if (ev.kind === "run") {
    // точно такую же команду дальше она запускает без вопросов (только в этом проекте)
    const always = Object.assign(document.createElement("button"), { className: "yes", textContent: "Разрешать всегда", title: "Только эту команду, символ в символ, только в этом проекте" });
    always.onclick = () => answer(true, true);
    row.append(always);
  }
  row.append(no);
  if (text) div.append(text);
  div.append(row);
  chat.scrollTop = chat.scrollHeight;
  quip(commit
    ? "[shy] Семпай, я придумала сообщение коммита. Глянешь и нажмёшь «Закоммитить»?"
    : ev.kind === "undo"
      ? "[shy] Семпай, отменить мой коммит? Нажми «Разрешить» в чате."
      : "[shy] Семпай, можно я кое-что запущу? Нажми «Разрешить» в чате.");
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  learnBtn.disabled = busy;
  for (const b of document.querySelectorAll("#actions button, #chips button")) b.disabled = busy;
  state.thinking = busy;
  mirror({ t: "busy", on: busy });
  if (busy) {
    state.emotion = "relaxed";
    thinkingEl.textContent = "хмм…";
    thinkingEl.hidden = false;
  } else {
    thinkingEl.hidden = true;
  }
}

// расходы на OpenRouter: итог за день в шапке; дорогой ответ — она смущённо признаётся
const SPENDY_TURN_USD = 0.05;
let turnCost = 0;
function showSpent(usd) {
  const el = $("spent");
  el.hidden = OBS || !(usd > 0);
  el.textContent = `💸 ${usd < 0.01 ? "<0,01" : usd.toFixed(2).replace(".", ",")} $`;
}

// ответ, который ещё пишется: текст в чате обновляется, готовые предложения сразу озвучиваются
let live = null; // { raw, pending, div }

function onDelta(text) {
  if (!live) {
    stopSpeaking();
    live = { raw: "", pending: "", div: addMessage("aiko", "") };
  }
  live.raw += text;
  live.pending += text;
  renderText(live.div, stripTags(live.raw).replace(/\[[a-z]*$/i, "")); // недописанный тег не показываем
  chat.scrollTop = chat.scrollHeight;
  feedSpeech(false);
}

/** Отдаём в озвучку законченные предложения; final — дописать хвост. */
function feedSpeech(final) {
  let m;
  while ((m = live.pending.match(/^[\s\S]*?(?:[.!?…]+(?=\s)|\n)/))) {
    enqueueSpeech(m[0]);
    live.pending = live.pending.slice(m[0].length);
  }
  if (final) {
    enqueueSpeech(live.pending);
    live.pending = "";
  }
}

function finishLive(kind) {
  if (!live) return false;
  feedSpeech(true);
  live.div.className = `msg ${kind}`;
  if (!stripTags(live.raw)) live.div.remove();
  live = null;
  return true;
}

async function ask(payload) {
  turnCost = 0;
  setBusy(true);
  stopSpeaking();
  live = null;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, mood, day: dayStats() }),
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
    mirror({ t: "thinking", text: ev.text });
    addMessage("tool", ev.text);
    narrate(ev);
  } else if (ev.type === "outfit") {
    if (ev.outfit) changeOutfit(ev.outfit);
  } else if (ev.type === "code") {
    showCode(ev);
    mirror({ t: "code", ev });
  } else if (ev.type === "diff") {
    showDiff(ev);
    mirror({ t: "diff", ev });
  } else if (ev.type === "diagram") {
    showDiagram(ev);
    mirror({ t: "diagram", ev });
  } else if (ev.type === "confirm") {
    askConfirm(ev);
  } else if (ev.type === "annotations") {
    if (ev.notes.length) {
      const where = ev.notes.slice(0, 4).map((n) => `\`${n.path}:${n.line}\``).join(", ");
      addMessage("tool", `📌 ${ev.notes.length} замечаний в редакторе: ${where}${ev.notes.length > 4 ? "…" : ""}`);
    }
  } else if (ev.type === "usage") {
    turnCost += ev.cost;
    showSpent(ev.today);
  } else if (ev.type === "head") {
    lastHead = ev.head; // HEAD сдвинула она сама (коммит или отмена) — реагировать не нужно
  } else if (ev.type === "reminder") {
    addReminder(ev.minutes, ev.text);
  } else if (ev.type === "quest") {
    renderQuest(ev.quest);
    celebrate(`+${ev.gained} XP · ${ev.quest.level}`);
    mirror({ t: "celebrate", text: `+${ev.gained} XP` });
  } else if (ev.type === "doc") {
    const div = addMessage("aiko", "ONBOARDING.md готов: ");
    div.append(Object.assign(document.createElement("a"), { href: ev.url, textContent: "скачать", download: "ONBOARDING.md" }));
  } else if (ev.type === "delta") {
    onDelta(ev.text);
  } else if (ev.type === "thought") {
    // текст перед вызовом инструментов — это она думает вслух
    lastNarration = performance.now();
    if (finishLive("tool") || !stripTags(ev.text)) return;
    addMessage("tool", stripTags(ev.text));
    quip(ev.text);
  } else if (ev.type === "reply") {
    setBusy(false);
    $("next").hidden = !touring;
    followUpUntil = performance.now() + FOLLOW_UP_MS; // можно ответить ей без «Айко»
    if (!finishLive("aiko")) {
      addMessage("aiko", stripTags(ev.text));
      speak(ev.text);
    }
    refreshStatus();
    if (turnCost > SPENDY_TURN_USD) {
      const cents = Math.round(turnCost * 100);
      addMessage("tool", `💸 этот ответ стоил ~${cents}¢`);
      enqueueSpeech(pick([
        `[shy] Ой, семпай... этот ответ обошёлся в ${cents} центов. Я больше не буду так много читать!`,
        `[surprised] Ого, ${cents} центов за один ответ! Я дорогая девочка, хе-хе.`,
      ]));
    }
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
  reactToWords(text);
  ask({ message: text });
});

learnBtn.onclick = () => {
  touring = false;
  addMessage("user", "Айко, изучи, пожалуйста, проект ✨");
  ask({ action: "learn" });
};

// экскурсия идёт, пока не запустили другое действие; «Дальше →» — быстрый ответ
let touring = false;
const ACTION_LABELS = {
  tour: "Айко, проведи экскурсию по коду 🗺",
  review: "Айко, посмотри мои изменения 🔍",
  doc: "Айко, напиши ONBOARDING.md для команды 📄",
  first_task: "Айко, помоги выбрать первую задачу 🎯",
  quiz: "Айко, устрой мне экзамен по проекту 🎓",
  commit: "Айко, помоги закоммитить ✅",
};
function bindAction(btn) {
  btn.onclick = () => {
    const action = btn.dataset.action;
    touring = action === "tour";
    $("next").hidden = true;
    addMessage("user", ACTION_LABELS[action] || `Айко, ${btn.textContent} ✨`);
    ask({ action });
  };
}
for (const btn of document.querySelectorAll("#actions button")) bindAction(btn);

/** Свои кнопки-задания из skills/*.md (и .aiko/skills/ проекта). */
function renderSkills(skills) {
  for (const old of document.querySelectorAll("#actions button.skill")) old.remove();
  for (const sk of skills || []) {
    const b = Object.assign(document.createElement("button"), {
      type: "button", className: "skill", textContent: sk.title, title: sk.hint || sk.title,
    });
    b.dataset.action = `skill:${sk.id}`;
    b.disabled = sendBtn.disabled;
    bindAction(b);
    $("actions").append(b);
  }
}
$("next").onclick = () => {
  $("next").hidden = true;
  addMessage("user", "Дальше →");
  ask({ message: "Дальше" });
};

let pttHeld = false;  // пробел зажат
let pttOwned = false; // микрофон включён только на время push-to-talk

// разговор голосом без кнопок: включил 🎤 — она слушает, сама понимает начало и конец фразы
const ear = new Ear({
  // пока она говорит, порог выше: так её собственный голос из динамиков не считается речью семпая
  sensitivity: () => (state.talking ? 2.5 : 1),
  onStart: () => {
    wakeUp();
    if (state.talking && !state.thinking) stopSpeaking(); // перебили — замолкаем
    state.tilt = 0.12; // прислушивается
  },
  onUtterance: async (wav, { level, held }) => {
    state.tilt = 0;
    if (sendBtn.disabled) return; // она занята ответом — фразу пропускаем
    try {
      const res = await fetch("/api/stt", { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });
      const { text: heard, error } = await res.json();
      if (error) throw new Error(error);
      if (!heard || sendBtn.disabled) return;
      const text = held ? heard : addressed(heard);
      if (text === null) return; // режим волшебного слова: это было не ей
      feelVoice(level);
      addMessage("user", `🎤 ${heard}`);
      if (!text) { // просто позвали по имени
        followUpUntil = performance.now() + FOLLOW_UP_MS;
        quip(pick(["[happy] Да, семпай?", "[surprised] М? Я тут!", "[nod] Слушаю!"]));
        return;
      }
      reactToWords(text);
      ask({ message: text });
    } catch (e) {
      addMessage("error", `Не расслышала: ${e.message}`);
    }
  },
});

// --- волшебное слово: микрофон слышит всё, но отвечает она только на «Айко, …» ---
const MIC_MODES = ["off", "all", "wake"];
const FOLLOW_UP_MS = 20_000; // после её ответа можно продолжить разговор без имени
const WAKE_RE = /(?<!\p{L})(?:айко|айка|аико|aiko)(?:-?(?:тян|chan))?(?!\p{L})[\s,.!:—-]*/iu;
let micMode = "off";
let followUpUntil = 0;

/** Текст для Айко без её имени; "" — только позвали; null — фраза была не ей. */
function addressed(heard) {
  if (micMode !== "wake") return heard;
  if (WAKE_RE.test(heard)) return heard.replace(WAKE_RE, "").trim();
  return performance.now() < followUpUntil ? heard : null;
}

// --- настроение от голоса: крик роняет его, спокойный тон чуть поднимает ---
const VOICE_KEY = "aiko-voice";
function feelVoice(level) {
  if (!(level > 0)) return;
  let profile;
  try { profile = JSON.parse(localStorage.getItem(VOICE_KEY)) || { avg: 0, n: 0 }; } catch { profile = { avg: 0, n: 0 }; }
  const ratio = profile.avg ? level / profile.avg : 1;
  if (profile.n >= 3 && ratio > 2.2) {
    shiftMood(-0.08);
    state.emotion = "surprised";
    motion.play("shake");
    quip(pick(["[surprised] Ай! Не кричи так, семпай, я же рядом.", "[sad] Ты чего такой громкий... я что-то сломала?"]));
    return; // крик в «обычную громкость» не записываем
  }
  if (profile.n >= 3 && ratio < 1.3) shiftMood(0.01);
  // обычная громкость семпая — скользящее среднее (первые фразы — простое среднее)
  const k = 1 / Math.min(profile.n + 1, 20);
  localStorage.setItem(VOICE_KEY, JSON.stringify({ avg: profile.avg * (1 - k) + level * k, n: profile.n + 1 }));
}

if (navigator.mediaDevices?.getUserMedia && window.AudioWorkletNode) {
  micBtn.hidden = false;
  const showMicMode = () => {
    micBtn.classList.toggle("on", micMode !== "off");
    micBtn.classList.toggle("wake", micMode === "wake");
    micBtn.textContent = micMode === "wake" ? "✨" : "🎤";
    micBtn.title = {
      off: "Клик — слушать всё время; пробел — держать и говорить",
      all: "Слушаю всё. Клик — отвечать только на «Айко, …»",
      wake: "Отвечаю только на «Айко, …». Клик — выключить микрофон",
    }[micMode];
  };
  showMicMode();
  // клик по кругу: выключен → слушает всё → только «Айко, …» → выключен
  micBtn.onclick = async () => {
    const next = MIC_MODES[(MIC_MODES.indexOf(micMode) + 1) % MIC_MODES.length];
    if (next === "off") {
      ear.stop();
      micMode = "off";
      showMicMode();
      return;
    }
    try {
      if (!ear.active) await ear.start();
      ear.vad = true;
      pttOwned = false;
      micMode = next;
      showMicMode();
      quip(next === "wake" ? "[nod] Хорошо, отвечаю, только когда позовёшь: «Айко, …»" : "[happy] Слушаю, семпай! Просто говори.");
    } catch (e) {
      addMessage("error", `Микрофон недоступен: ${e.message}`);
    }
  };

  // push-to-talk: держишь пробел — она слушает, отпустил — фраза уходит
  const typing = (el) => el?.closest?.("input, textarea, [contenteditable]");
  document.addEventListener("keydown", async (e) => {
    if (e.code !== "Space" || e.repeat || typing(e.target)) return;
    e.preventDefault();
    pttHeld = true;
    if (!ear.active) {
      try {
        await ear.start();
        ear.vad = false; // микрофон включили только ради push-to-talk — сам он не слушает
        pttOwned = true;
      } catch (err) {
        pttHeld = false;
        addMessage("error", `Микрофон недоступен: ${err.message}`);
        return;
      }
    }
    if (!pttHeld) return endPtt(); // пробел отпустили, пока включался микрофон
    ear.hold();
    thinkingEl.textContent = "🎙 говори, я слушаю…";
    thinkingEl.hidden = false;
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space" && pttHeld) {
      pttHeld = false;
      endPtt();
    }
  });
}

function endPtt() {
  ear.release();
  if (!state.thinking) thinkingEl.hidden = true;
  if (pttOwned) {
    ear.stop(); // запись уже отдана в onUtterance — микрофон можно выключить
    pttOwned = false;
  }
}

let greeted = OBS;
async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    $("meta").textContent = `${s.project} · ${s.model}`;
    showSpent(s.spent_today);
    serverTTS = s.tts;
    projectModelUrl = s.model_url;
    if (s.outfit && !outfit) changeOutfit(s.outfit, false);
    learnBtn.hidden = false;
    learnBtn.textContent = !s.has_notes ? "✨ Изучить проект"
      : s.stale_commits ? `🔄 Переизучить проект (+${s.stale_commits} коммитов)` : "🔄 Переизучить проект";
    learnBtn.classList.toggle("stale", Boolean(s.stale_commits));
    renderQuest(s.quest);
    renderSkills(s.skills);
    if (!greeted) {
      greeted = true;
      // чат с прошлого раза: сервер помнит последние реплики
      for (const m of s.transcript || []) addMessage(m.role === "user" ? "user" : "aiko", stripTags(m.content));
      const hello = s.has_notes && s.stale_commits
        ? `[happy] [wave] С возвращением, семпай! [surprised] Ого, в ${s.project} уже ${s.stale_commits} новых коммитов с тех пор, как я его изучала. Нажми «Переизучить», и я догоню!`
        : s.has_notes
        ? `[happy] [wave] С возвращением, семпай! Я помню проект ${s.project} по своим заметкам. Спрашивай что угодно!`
        : `[happy] [wave] Привет, семпай! Я Айко, твоя новая коллега. [surprised] Проект ${s.project} я ещё не видела — нажми «Изучить проект», и я всё-всё разберу!`;
      addMessage("aiko", stripTags(hello));
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

// ============================================================
// Живость: курсор, клики, скука, сон, перебивание, мысли вслух
// ============================================================

const BORED_AFTER_MS = 3 * 60_000; // столько молчания — и она заговорит сама
const SLEEP_AFTER_MS = 8 * 60_000; // а потом уснёт
const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

let lastActivity = performance.now();
let boredSpoken = false;
let lastNarration = 0;
let lastPoke = 0;

function wakeUp() {
  lastActivity = performance.now();
  boredSpoken = false;
  if (!state.sleeping) return;
  state.sleeping = false;
  mirror({ t: "sleep", on: false });
  stage.classList.remove("paused");
  thinkingEl.hidden = true;
  state.emotion = "surprised";
  quip(pick([
    "[surprised] [shake] А? Я не сплю! Я просто анализировала код с закрытыми глазами!",
    "[surprised] Ой, семпай! Я на секундочку прикрыла глаза, честно-честно.",
    "[relaxed] [stretch] Ммм... доброе утро, семпай. Что я пропустила?",
  ]));
}

function fallAsleep() {
  if (state.sleeping || state.thinking || state.talking) return;
  state.sleeping = true;
  mirror({ t: "sleep", on: true });
  stage.classList.add("paused");
  state.emotion = "relaxed";
  thinkingEl.textContent = "💤 Zzz…";
  thinkingEl.hidden = false;
}

for (const ev of ["pointerdown", "keydown", "wheel"]) document.addEventListener(ev, wakeUp);

// глаза следят за курсором: точка на плоскости чуть перед лицом
const raycaster = new THREE.Raycaster();
const ndc = new THREE.Vector2();
stage.addEventListener("pointermove", (e) => {
  const r = canvas.getBoundingClientRect();
  ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(ndc, camera);
  const pos = raycaster.ray.at(Math.max(0.1, camera.position.z - 0.4), new THREE.Vector3());
  state.pointer = { pos, until: performance.now() + 2500 };
  lastActivity = performance.now();
});

// клик по ней: голова — погладить, тело — ткнуть
canvas.addEventListener("pointerdown", (e) => {
  if (!vrm || state.thinking) return;
  const now = performance.now();
  if (now - lastPoke < 1200) return;
  const r = canvas.getBoundingClientRect();
  ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(ndc, camera);
  const hit = raycaster.intersectObject(vrm.scene, true)[0];
  if (!hit) return;
  lastPoke = now;
  const headY = vrm.humanoid.getNormalizedBoneNode("neck").getWorldPosition(new THREE.Vector3()).y;
  if (hit.point.y > headY + 0.04) {
    shiftMood(0.12);
    state.emotion = "happy";
    state.eyesClosedUntil = now + 2200;
    state.tilt = 0.15;
    setTimeout(() => { state.tilt = 0; }, 2200);
    quip(pick(["[happy] Э-хе-хе...", "[happy] [shy] Ну семпай, не при всех же...", "[relaxed] Мур... ещё чуть-чуть.", "[happy] Хи-хи, щекотно!"]));
  } else {
    shiftMood(-0.15);
    state.emotion = "surprised";
    quip(pick(["[surprised] Ай!", "[angry] [shake] Эй! Руки, семпай!", "[surprised] Чего тыкаешь?", "[angry] Я вообще-то работаю!"]));
  }
});

// вставили стектрейс — она заранее оживляется (разбирать его по коду будет сервер)
const TRACE_HINT = /Traceback \(most recent call last\)|^\s+at .+:\d+|^goroutine \d+ \[|^panic: |panicked at/m;
input.addEventListener("paste", (e) => {
  const text = e.clipboardData?.getData("text") || "";
  if (TRACE_HINT.test(text)) quip(pick(["[surprised] Ой, стектрейс! Отправляй — найду, где болит.", "[think] Так-так, ошибочка... Жми «отправить», разберу по коду."]));
});

// семпай начал печатать — замолкаем, как на стриме, когда чат перебивает
input.addEventListener("keydown", () => {
  if (state.talking && !state.thinking) stopSpeaking();
});

// мысли вслух, пока она копается в проекте (не чаще раза в 9 секунд)
function narrate(ev) {
  const now = performance.now();
  if (now - lastNarration < 9000) return;
  lastNarration = now;
  const file = (ev.text.match(/читаю (.+)/) || [])[1];
  let line;
  if (file && /package\.json|pyproject|cargo\.toml|go\.mod|requirements/i.test(file)) {
    line = pick(["[nod] Так, зависимости... посмотрим, на чём всё держится.", "[think] Ага, манифест. Сейчас узнаем стек!"]);
  } else if (file && /readme/i.test(file)) {
    line = pick(["[happy] О, README! Кто-то позаботился о новичках.", "[relaxed] Читаю README... ну хоть кто-то пишет документацию."]);
  } else if (file && /test/i.test(file)) {
    line = pick(["[happy] Тесты! Обожаю, когда есть тесты.", "[nod] Посмотрим, что тут проверяют."]);
  } else if (ev.text.startsWith("ищу")) {
    line = pick(["[think] Ищу-ищу...", "[think] Где же это...", "Хмм, давай поищем."]);
  } else if (ev.text.startsWith("смотрю папку")) {
    line = pick(["Так-так, что тут у нас...", "[think] Интересная структура.", "Ого, сколько папок."]);
  } else if (file) {
    line = pick(["Хмм...", "Интересненько.", "[nod] Понятно-понятно.", "[think] А вот это любопытно.", "Так, запоминаю."]);
  } else {
    return;
  }
  quip(line);
}

// спонтанное поведение: оглянуться, потянуться, заскучать, уснуть
setInterval(() => {
  if (!vrm || document.hidden) return;
  const now = performance.now();
  const idleFor = now - lastActivity;
  if (state.thinking || state.talking || state.sleeping || motion.busy) return;

  if (OBS) {
    // зритель только оглядывается, а спит и болтает вслед за основной вкладкой
  } else if (idleFor > SLEEP_AFTER_MS) {
    fallAsleep();
  } else if (idleFor > BORED_AFTER_MS && !boredSpoken && !sendBtn.disabled) {
    boredSpoken = true;
    shiftMood(-0.05);
    ask({ action: "idle" });
  } else if (Math.random() < 0.35) {
    // мелкие движения, чтобы не стояла статуей
    const r = Math.random();
    if (r < 0.5) {
      state.gaze = {
        pos: new THREE.Vector3((Math.random() - 0.5) * 2, cameraTarget.y + (Math.random() - 0.3) * 0.6, camera.position.z),
        until: now + 1200 + Math.random() * 1500,
      };
    } else if (r < 0.75) {
      motion.play("nod");
    } else if (r < 0.9) {
      state.tilt = (Math.random() - 0.5) * 0.25;
      setTimeout(() => { state.tilt = 0; }, 2000);
    } else {
      motion.play("stretch");
    }
  }
}, 5000);

// сезонные украшения: Хэллоуин — летучие мыши, Новый год — снег, весна — сакура
function decorateSeason(d = new Date()) {
  const md = (d.getMonth() + 1) * 100 + d.getDate();
  const season = md >= 1020 && md <= 1101 ? { emoji: ["🦇", "🦇", "🎃"], cls: "bats", n: 7 }
    : md >= 1215 || md <= 110 ? { emoji: ["❄", "❅", "❆"], cls: "", n: 16 }
    : md >= 320 && md <= 430 ? { emoji: ["🌸", "🌸", "💮"], cls: "", n: 12 }
    : null;
  if (!season) return;
  const box = $("season");
  box.className = season.cls;
  for (let i = 0; i < season.n; i++) {
    const s = document.createElement("span");
    s.textContent = pick(season.emoji);
    const dur = 8 + Math.random() * 10;
    s.style.cssText = season.cls === "bats"
      ? `top:${5 + Math.random() * 55}%;--size:${20 + Math.random() * 16}px;--dur:${dur}s;--delay:${-Math.random() * dur}s`
      : `left:${Math.random() * 100}%;--size:${12 + Math.random() * 14}px;--dur:${dur}s;--delay:${-Math.random() * dur}s;--drift:${(Math.random() - 0.5) * 160}px`;
    box.append(s);
  }
}

// ---------- напоминания: живут в localStorage, переживают перезагрузку страницы ----------
const REMINDERS_KEY = "aiko-reminders";
const loadReminders = () => {
  try { return JSON.parse(localStorage.getItem(REMINDERS_KEY)) || []; } catch { return []; }
};
const saveReminders = (list) => localStorage.setItem(REMINDERS_KEY, JSON.stringify(list));
const hhmm = (ms) => new Date(ms).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });

function addReminder(minutes, text) {
  const due = Date.now() + minutes * 60_000;
  saveReminders([...loadReminders(), { due, text }]);
  addMessage("tool", `⏰ напомню в ${hhmm(due)}: ${text}`);
  // системное уведомление — на случай, если вкладка будет свёрнута
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
}

// статистика дня для вечернего итога (напоминания живут только в браузере)
const DAY_KEY = "aiko-day";
function dayStats() {
  const today = new Date().toDateString();
  try {
    const d = JSON.parse(localStorage.getItem(DAY_KEY));
    if (d?.date === today) return d;
  } catch {}
  return { date: today, reminders: 0 };
}

function checkReminders() {
  const now = Date.now();
  const list = loadReminders();
  const due = list.filter((r) => r.due <= now);
  if (!due.length) return;
  saveReminders(list.filter((r) => r.due > now));
  const day = dayStats();
  localStorage.setItem(DAY_KEY, JSON.stringify({ ...day, reminders: day.reminders + due.length }));
  for (const r of due) {
    wakeUp();
    addMessage("aiko", `⏰ ${r.text}`);
    enqueueSpeech(`[surprised] [wave] Семпай! ${r.text}`); // не перебиваем, если она говорит
    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      new Notification("Айко-тян ⏰", { body: r.text });
    }
  }
}
if (!OBS) setInterval(checkReminders, 10_000);

// ---------- 👀 режим наблюдения: заметить правки семпая и предложить ревью ----------
const WATCH_EVERY_MS = 30_000;
const OFFER_COOLDOWN_MS = 5 * 60_000;
const COMMIT_TALK_COOLDOWN_MS = 2 * 60_000; // серия коммитов подряд — одна реплика
let watching = localStorage.getItem("aiko-watch") === "1";
let lastSig = null;    // что уже видели
let lastHead = null;   // последний известный коммит: сменился — семпай закоммитил
let lastCommitTalk = -Infinity;
let pendingSig = null; // новые правки ждут, пока «устоятся» (два опроса подряд без изменений)
let lastOffer = -Infinity;

function setWatching(on) {
  watching = on;
  localStorage.setItem("aiko-watch", on ? "1" : "0");
  $("watch").classList.toggle("on", on);
  lastSig = pendingSig = lastHead = null;
}
$("watch").onclick = () => {
  setWatching(!watching);
  quip(watching ? "[nod] Буду поглядывать за твоими правками, семпай." : "[relaxed] Ладно, не подглядываю.");
};
setWatching(watching);

async function pollChanges() {
  if (!watching || OBS || document.hidden || state.thinking || state.sleeping) return;
  let c;
  try { c = await (await fetch("/api/changes")).json(); } catch { return; }
  if (!c.git) {
    setWatching(false);
    addMessage("error", "Следить за правками умею только в git-проектах.");
    return;
  }
  if (lastSig === null) { lastSig = c.sig; lastHead = c.head; return; } // первый опрос — точка отсчёта
  if (c.head && c.head !== lastHead) {
    // новый коммит: правки ушли в историю, предлагать ревью уже поздно — лучше прокомментировать
    lastHead = c.head;
    lastSig = c.sig;
    pendingSig = null;
    $("offer").hidden = true;
    if (performance.now() - lastCommitTalk < COMMIT_TALK_COOLDOWN_MS || sendBtn.disabled) return;
    lastCommitTalk = performance.now();
    ask({ action: "new_commit" });
    return;
  }
  if (c.sig === lastSig) { pendingSig = null; return; }
  if (c.sig !== pendingSig) { pendingSig = c.sig; return; }
  lastSig = c.sig;
  pendingSig = null;
  if (!c.sig || performance.now() - lastOffer < OFFER_COOLDOWN_MS || sendBtn.disabled) return;
  lastOffer = performance.now();
  const more = c.files.length > 2 ? ` и ещё ${c.files.length - 2}` : "";
  const names = c.files.slice(0, 2).map((f) => `\`${f}\``).join(", ") + more;
  addMessage("aiko", `👀 Вижу правки: ${names}. Глянуть?`);
  quip(`[think] О, семпай что-то поменял${c.files.length === 1 ? ` в ${c.files[0].split("/").pop()}` : ""}. Хочешь, гляну?`);
  $("offer").hidden = false;
}
if (!OBS) setInterval(pollChanges, WATCH_EVERY_MS);
$("offer").onclick = () => {
  $("offer").hidden = true;
  addMessage("user", ACTION_LABELS.review);
  ask({ action: "review" });
};

// ---------- онбординг-квест ----------
const questList = $("quest-list");
$("quest-bar").onclick = () => { questList.hidden = !questList.hidden; };

function renderQuest(q) {
  if (!q || OBS) return;
  $("quest-bar").hidden = false;
  $("quest-level").textContent = `★ ${q.level}`;
  $("quest-xp").textContent = q.next_level_xp ? `${q.xp} / ${q.next_level_xp} XP` : `${q.xp} XP`;
  $("quest-fill").style.width = `${Math.round((q.xp / q.max_xp) * 100)}%`;
  questList.replaceChildren(...q.steps.map((st) => {
    const li = document.createElement("li");
    li.className = st.done ? "done" : "";
    li.textContent = `${st.done ? "✅" : "⬜"} ${st.title}`;
    const xp = document.createElement("span");
    xp.textContent = `${st.xp} XP`;
    li.append(xp);
    return li;
  }));
}

/** Тост и конфетти; Айко подпрыгивает от радости. */
function celebrate(text) {
  const toast = $("toast");
  toast.textContent = `🎉 ${text}`;
  toast.hidden = false;
  clearTimeout(celebrate.timer);
  celebrate.timer = setTimeout(() => { toast.hidden = true; }, 3500);
  for (let i = 0; i < 18; i++) {
    const c = document.createElement("span");
    c.className = "confetti";
    c.textContent = pick(["🎉", "✨", "⭐", "💖", "🌸"]);
    const angle = Math.random() * Math.PI * 2;
    const dist = 120 + Math.random() * 220;
    c.style.cssText = `--dx:${Math.cos(angle) * dist}px;--dy:${Math.sin(angle) * dist}px;--rot:${Math.random() * 720 - 360}deg`;
    stage.append(c);
    setTimeout(() => c.remove(), 1700);
  }
  motion.play("hop");
  shiftMood(0.1);
}

/** Похвала и грубость влияют на настроение (а модель узнаёт о нём из mood). */
function reactToWords(text) {
  if (/молодец|умниц|спасибо|класс|супер|обожаю|люблю|хорошая|лучшая|thank/i.test(text)) shiftMood(0.1);
  if (/дура|тупая|глупая|бесишь|заткнись|отстань|бесполезн/i.test(text)) shiftMood(-0.3);
}

// настроение само остывает к нулю: примерно на 0.05 каждые 10 минут
setInterval(() => { if (Math.abs(mood) > 0.01) shiftMood(-Math.sign(mood) * 0.05); }, 600_000);
shiftMood(0);

// переключение проектов: список недавних + любая папка; после смены — перезагрузка страницы
const projectMenu = $("project-menu");
$("projects").onclick = async () => {
  if (!projectMenu.hidden) { projectMenu.hidden = true; return; }
  const s = await (await fetch("/api/status")).json();
  projectMenu.replaceChildren();
  for (const path of s.recent) {
    const b = Object.assign(document.createElement("button"), { textContent: `📁 ${path}`, title: path });
    if (path === s.path) b.className = "current";
    b.onclick = () => switchProject(path);
    projectMenu.append(b);
  }
  const other = Object.assign(document.createElement("button"), { textContent: "➕ Другая папка…" });
  other.onclick = () => {
    const path = prompt("Путь к проекту:", s.path);
    if (path) switchProject(path);
  };
  projectMenu.append(other);
  renderAllowed(s.allowed);
  projectMenu.hidden = false;
};

/** Команды «разрешать всегда» текущего проекта — каждую можно забрать обратно. */
function renderAllowed(list) {
  projectMenu.querySelector(".allowed")?.remove();
  if (!list?.length) return;
  const box = Object.assign(document.createElement("div"), { className: "allowed" });
  box.append(Object.assign(document.createElement("div"), { className: "allowed-title", textContent: "🔓 Запускает без вопросов:" }));
  for (const command of list) {
    const b = Object.assign(document.createElement("button"), { textContent: `✕ ${command}`, title: "Снова спрашивать перед запуском" });
    b.onclick = async () => {
      const res = await fetch("/api/allowed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command }),
      }).catch(() => null);
      if (res?.ok) renderAllowed((await res.json()).allowed);
    };
    box.append(b);
  }
  projectMenu.append(box);
}

async function switchProject(path) {
  projectMenu.hidden = true;
  const res = await fetch("/api/project", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const data = await res.json();
  if (!res.ok) {
    addMessage("error", data.error);
    return;
  }
  location.reload(); // новая модель, образ и заметки — проще и чище начать страницу заново
}

// окно OBS: принимаем события основной вкладки
if (OBS) {
  const events = new EventSource("/api/events");
  events.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.t === "say") {
      if (ev.emotion) state.emotion = ev.emotion;
      if (ev.gesture) motion.play(ev.gesture);
      if (ev.text) {
        subtitles.textContent = ev.text.replace(/`/g, "");
        state.talking = true; // голос звучит в основной вкладке — тут рот двигается по имитации
      }
    } else if (ev.t === "quiet") {
      state.talking = false;
      setTimeout(() => { if (!state.talking) subtitles.textContent = ""; }, 2500);
    } else if (ev.t === "busy") {
      state.thinking = ev.on;
      thinkingEl.textContent = "хмм…";
      thinkingEl.hidden = !ev.on;
    } else if (ev.t === "thinking") {
      thinkingEl.textContent = ev.text;
    } else if (ev.t === "code") {
      showCode(ev.ev);
    } else if (ev.t === "diff") {
      showDiff(ev.ev);
    } else if (ev.t === "diagram") {
      showDiagram(ev.ev);
    } else if (ev.t === "outfit") {
      changeOutfit(ev.outfit, ev.animated);
    } else if (ev.t === "celebrate") {
      celebrate(ev.text);
    } else if (ev.t === "sleep") {
      state.sleeping = ev.on;
      thinkingEl.textContent = "💤 Zzz…";
      thinkingEl.hidden = !ev.on;
    }
  };
}

// VS Code: расширение присылает выделенный код через postMessage родительского окна
if (EMBED === "vscode") {
  window.addEventListener("message", (e) => {
    if (e.source !== window.parent || !String(e.origin).startsWith("vscode-webview://")) return;
    const { type, text, label } = e.data || {};
    if (type !== "aiko-ask" || typeof text !== "string" || sendBtn.disabled) return;
    addMessage("user", label || text.slice(0, 200));
    ask({ message: text.slice(0, 20000) });
  });
}

decorateSeason();
// сначала узнаём, нет ли своей модели для проекта, потом грузим
refreshStatus().finally(loadDefaultModel);
