// «Уши» Айко: постоянно слушают микрофон и сами понимают, когда семпай заговорил и замолчал.
// Детектор речи — по громкости относительно фонового шума; запись уходит на сервер в WAV 16 кГц.

const TARGET_RATE = 16000;
const START_AFTER_S = 0.12;   // столько громкого звука подряд — значит, говорят
const STOP_AFTER_MS = 900;    // столько тишины — фраза закончилась
const MIN_SPEECH_S = 0.4;     // короче — это кашель или стук, а не речь
const MAX_UTTERANCE_S = 30;   // дольше не пишем, чтобы не копить память
const PREROLL_S = 0.3;        // начало фразы, сказанное до срабатывания детектора

export class Ear {
  /**
   * onStart — семпай начал говорить; onUtterance(wavBlob, { level, held }) — фраза целиком,
   * средняя громкость речи в ней (для настроения: крик или спокойный тон) и была ли она
   * сказана с зажатой кнопкой (тогда она точно адресована Айко);
   * sensitivity() — множитель порога (выше, пока Айко сама говорит, чтобы не слышать своё эхо).
   */
  constructor({ onStart, onUtterance, sensitivity = () => 1 }) {
    Object.assign(this, { onStart, onUtterance, sensitivity });
    this.active = false;
    this.vad = true;     // сама находит начало фразы; false — только push-to-talk
    this.forced = false; // кнопку держат — пишем, пока не отпустят
  }

  /** Push-to-talk: начать запись прямо сейчас (с кусочком звука до нажатия). */
  hold() {
    if (!this.active || this.forced) return;
    this.forced = true;
    this.held = true;
    if (!this.recording) this.#begin(performance.now());
  }

  /** Кнопку отпустили — отправляем сказанное. */
  release() {
    if (!this.forced) return;
    this.forced = false;
    if (this.recording) this.#finish();
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule("mic-worklet.js");
    this.rate = this.ctx.sampleRate;
    this.node = new AudioWorkletNode(this.ctx, "aiko-tap");
    this.ctx.createMediaStreamSource(this.stream).connect(this.node); // в динамики не выводим
    this.node.port.onmessage = (e) => this.#frame(e.data);
    this.noise = 0.01;
    this.#reset();
    this.active = true;
  }

  stop() {
    this.active = false;
    this.node?.port.close();
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close();
    this.node = this.stream = this.ctx = null;
    this.#reset();
  }

  #reset() {
    this.recording = false;
    this.forced = false;
    this.held = false;
    this.chunks = [];
    this.preroll = [];
    this.loud = 0;
    this.voiced = 0;
    this.voicedEnergy = 0; // сумма rms² по кадрам с голосом — для средней громкости фразы
    this.samples = 0;
  }

  #frame(frame) {
    let sum = 0;
    for (const v of frame) sum += v * v;
    const rms = Math.sqrt(sum / frame.length);
    const threshold = Math.max(0.015, this.noise * 3) * this.sensitivity();
    const now = performance.now();

    if (!this.recording) {
      // подстраиваемся под шум комнаты только в тишине — иначе речь сама поднимет порог
      if (rms < threshold) this.noise = Math.min(0.05, this.noise * 0.998 + rms * 0.002);
      this.preroll.push(frame);
      while (this.preroll.length * 128 > this.rate * PREROLL_S) this.preroll.shift();
      this.loud = rms > threshold ? this.loud + frame.length : 0;
      if (this.vad && this.loud >= this.rate * START_AFTER_S) this.#begin(now);
      return;
    }

    this.chunks.push(frame);
    this.samples += frame.length;
    if (rms > threshold * 0.6) {
      this.lastVoice = now;
      this.voiced += frame.length;
      this.voicedEnergy += rms * rms * frame.length;
    }
    const silent = !this.forced && now - this.lastVoice > STOP_AFTER_MS;
    if (silent || this.samples > this.rate * MAX_UTTERANCE_S) this.#finish();
  }

  #begin(now) {
    this.recording = true;
    this.chunks = this.preroll;
    this.samples = this.chunks.length * 128;
    this.preroll = [];
    this.lastVoice = now;
    this.onStart?.();
  }

  #finish() {
    const { chunks, voiced, voicedEnergy, held } = this;
    this.#reset();
    if (voiced < this.rate * MIN_SPEECH_S) return;
    this.onUtterance?.(encodeWav(downsample(chunks, this.rate)), { level: Math.sqrt(voicedEnergy / voiced), held });
  }
}

/** Склеиваем кусочки и понижаем частоту до 16 кГц (усредняя соседние сэмплы). */
function downsample(chunks, rate) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const input = new Float32Array(total);
  let off = 0;
  for (const c of chunks) { input.set(c, off); off += c.length; }
  const ratio = rate / TARGET_RATE;
  const out = new Float32Array(Math.floor(total / ratio));
  for (let i = 0; i < out.length; i++) {
    const from = Math.floor(i * ratio);
    const to = Math.min(total, Math.floor((i + 1) * ratio));
    let s = 0;
    for (let j = from; j < to; j++) s += input[j];
    out[i] = s / Math.max(1, to - from);
  }
  return out;
}

function encodeWav(samples) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, TARGET_RATE, true); v.setUint32(28, TARGET_RATE * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}
