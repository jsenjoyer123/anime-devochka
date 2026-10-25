// Тело Айко: поза покоя, дыхание и жесты, которые плавно смешиваются друг с другом.
// Поза — это { кость: [x, y, z] } в нормализованном скелете VRM (T-поза, лицом к +Z).
// Для правой руки отрицательный поворот по Z поднимает её, для левой — положительный.

const BONES = [
  "spine", "chest", "neck", "head",
  "leftUpperArm", "rightUpperArm", "leftLowerArm", "rightLowerArm", "leftHand", "rightHand",
];

const smooth = (x) => x * x * (3 - 2 * x);

/** Огибающая жеста: плавно входим, держим, плавно выходим. */
function envelope(t, dur, fade = 0.3) {
  if (t < fade) return smooth(t / fade);
  if (t > dur - fade) return smooth(Math.max(0, dur - t) / fade);
  return 1;
}

/** Поза покоя с дыханием; s — текущее состояние (говорит, думает, спит). */
export function idlePose(t, s) {
  const breath = Math.sin(t * (s.sleeping ? 0.9 : 1.6));
  const pose = {
    spine: [0, 0, Math.sin(t * 0.7) * 0.02],
    chest: [breath * 0.015, 0, 0],
    neck: [0, Math.sin(t * 0.5) * 0.05, Math.sin(t * 0.9) * 0.04],
    head: [0, 0, 0],
    leftUpperArm: [0, 0, -1.2 + breath * 0.02],
    rightUpperArm: [0, 0, 1.2 - breath * 0.02],
    leftLowerArm: [0, -0.2, 0],
    rightLowerArm: [0, 0.2, 0],
    leftHand: [0, 0, 0],
    rightHand: [0, 0, 0],
  };
  if (s.talking) {
    pose.neck[0] += Math.sin(t * 3.1) * 0.03;
    pose.neck[1] += Math.sin(t * 2.3) * 0.04;
  }
  if (s.thinking) pose.neck[2] += 0.18;
  if (s.tilt) pose.neck[2] += s.tilt; // наклон головы от реакций (погладили и т.п.)
  if (s.sleeping) {
    pose.neck[0] += 0.35;
    pose.neck[2] += 0.2;
    pose.spine[0] += 0.08;
  }
  return pose;
}

// Каждый жест: длительность и поза от времени с начала жеста.
// Жесты задают только те кости, которые двигают; остальное берётся из позы покоя.
export const GESTURES = {
  wave: {
    dur: 2.2,
    pose: (t) => ({
      rightUpperArm: [0, 0.35, -0.15],
      rightLowerArm: [0, 0, -1.55 + Math.sin(t * 11) * 0.35],
      rightHand: [0, 0, Math.sin(t * 11) * 0.2],
      neck: [0, 0, -0.08],
    }),
  },
  cheer: {
    dur: 1.8,
    pose: (t) => {
      const b = Math.sin(t * 9) * 0.15;
      return {
        leftUpperArm: [0, 0, 1.0 + b],
        rightUpperArm: [0, 0, -1.0 - b],
        leftLowerArm: [0, 0, 0.45],
        rightLowerArm: [0, 0, -0.45],
        neck: [-0.08, 0, 0],
      };
    },
  },
  clap: {
    dur: 1.8,
    pose: (t) => {
      const c = Math.abs(Math.sin(t * 9)) * 0.28;
      return {
        leftUpperArm: [0, -0.9, -0.85],
        rightUpperArm: [0, 0.9, 0.85],
        leftLowerArm: [0, -1.75 + c, 0],
        rightLowerArm: [0, 1.75 - c, 0],
      };
    },
  },
  think: {
    dur: 2.8,
    pose: () => ({
      rightUpperArm: [0, 0.9, 1.0],
      rightLowerArm: [0, 2.45, 0],
      rightHand: [0, 0, 0.3],
      leftUpperArm: [0, -0.6, -1.2],
      leftLowerArm: [0, -1.5, 0],
      neck: [-0.06, 0, 0.16],
    }),
  },
  shy: {
    dur: 2.6,
    pose: (t) => ({
      leftUpperArm: [0, -0.7, -1.25],
      rightUpperArm: [0, 0.7, 1.25],
      leftLowerArm: [0, -1.25, 0],
      rightLowerArm: [0, 1.25, 0],
      neck: [0.28, Math.sin(t * 2) * 0.08, 0.12],
      spine: [0, 0, Math.sin(t * 2) * 0.05],
    }),
  },
  nod: {
    dur: 1.1,
    pose: (t) => ({ neck: [Math.max(0, Math.sin(t * 11)) * 0.2, 0, 0] }),
  },
  shake: {
    dur: 1.2,
    pose: (t) => ({ neck: [0, Math.sin(t * 13) * 0.25 * (1 - t / 1.2), 0] }),
  },
  bow: {
    dur: 2.0,
    pose: () => ({
      spine: [0.35, 0, 0],
      neck: [0.2, 0, 0],
      leftUpperArm: [0, -0.3, -1.3],
      rightUpperArm: [0, 0.3, 1.3],
    }),
  },
  stretch: {
    dur: 2.6,
    pose: (t) => ({
      leftUpperArm: [0, 0, 1.25],
      rightUpperArm: [0, 0, -1.25],
      leftLowerArm: [0, 0, 0.3],
      rightLowerArm: [0, 0, -0.3],
      spine: [-0.08, 0, Math.sin(t * 2.4) * 0.12],
      neck: [-0.15, 0, 0],
    }),
  },
  hop: { dur: 0.9, pose: () => ({}), hop: true }, // прыжок двигает всю модель — см. hopOffset
};

export const GESTURE_NAMES = Object.keys(GESTURES);

export class Motion {
  constructor() {
    this.current = null; // { name, t }
    this.smoothed = Object.fromEntries(BONES.map((b) => [b, [0, 0, 0]]));
  }

  /** Сразу встать в позу без сглаживания (после загрузки модели). */
  reset(pose) {
    this.current = null;
    for (const b of BONES) this.smoothed[b] = [...pose[b]];
  }

  play(name) {
    if (GESTURES[name]) this.current = { name, t: 0 };
  }

  get busy() {
    return this.current !== null;
  }

  /** Вертикальное смещение модели для прыжка. */
  hopOffset() {
    if (!this.current || !GESTURES[this.current.name].hop) return 0;
    const p = this.current.t / GESTURES[this.current.name].dur;
    return Math.sin(Math.min(p, 1) * Math.PI) * 0.06;
  }

  /** Смешивает позу покоя с текущим жестом, сглаживает и выставляет кости. */
  update(dt, base, getBone) {
    const target = base;
    if (this.current) {
      const g = GESTURES[this.current.name];
      this.current.t += dt;
      const w = envelope(this.current.t, g.dur);
      const gp = g.pose(this.current.t);
      for (const [bone, rot] of Object.entries(gp)) {
        const b = target[bone];
        // наклоны головы и корпуса складываем с позой покоя, руки — заменяем
        const additive = bone === "neck" || bone === "spine" || bone === "head";
        target[bone] = b.map((v, i) => (additive ? v + rot[i] * w : v + (rot[i] - v) * w));
      }
      if (this.current.t >= g.dur) this.current = null;
    }

    const k = 1 - Math.exp(-14 * dt);
    for (const name of BONES) {
      const node = getBone(name);
      if (!node) continue;
      const s = this.smoothed[name];
      const tg = target[name];
      for (let i = 0; i < 3; i++) s[i] += (tg[i] - s[i]) * k;
      node.rotation.set(s[0], s[1], s[2]);
    }
  }
}
