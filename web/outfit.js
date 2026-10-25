// Гардероб Айко: перекраска одежды VRoid-модели, аксессуары из примитивов и значок на груди.
// Ничего не загружаем — всё строится из пары десятков полигонов, старое освобождается.
import * as THREE from "three";

const SLOTS = [
  { re: /bottom|skirt|pants/i, key: "bottom_color" },
  { re: /shoe|boot|sock/i, key: "shoes_color" },
  { re: /cloth|top|onepiece|outer|dress/i, key: "top_color" }, // всё остальное из одежды — верх
];

let current = null; // { group, badge } — то, что надето сейчас

/** Сначала снимаем всё, что надето, и возвращаем исходные цвета. */
export function removeOutfit(vrm) {
  if (current) {
    for (const obj of [current.group, current.badge]) {
      if (!obj) continue;
      obj.removeFromParent();
      obj.traverse((o) => {
        o.geometry?.dispose();
        o.material?.map?.dispose();
        o.material?.dispose();
      });
    }
    current = null;
  }
  vrm?.scene.traverse((o) => {
    for (const m of materialsOf(o)) {
      const orig = m.userData.aikoOrig;
      if (!orig) continue;
      m.color?.copy(orig.color);
      if (orig.shade) m.shadeColorFactor.copy(orig.shade);
    }
  });
}

/** Надеть образ. Вызывать, когда vrm.scene не повёрнута (поза покоя по Y). */
export function applyOutfit(vrm, outfit) {
  removeOutfit(vrm);
  if (!vrm || !outfit) return;

  recolor(vrm, outfit);
  vrm.scene.updateMatrixWorld(true);
  const head = measureHead(vrm);
  const headBone = vrm.humanoid.getRawBoneNode("head");

  const group = buildAccessory(outfit.accessory, head, new THREE.Color(outfit.accessory_color));
  if (group && headBone) headBone.attach(group); // attach сохраняет мировое положение

  const badge = outfit.badge_text ? buildBadge(vrm, outfit) : null;
  current = { group, badge };
}

function materialsOf(o) {
  if (!o.isMesh) return [];
  return Array.isArray(o.material) ? o.material : [o.material];
}

function recolor(vrm, outfit) {
  vrm.scene.traverse((o) => {
    for (const m of materialsOf(o)) {
      if (!/cloth/i.test(m.name) || !m.color) continue;
      const slot = SLOTS.find((s) => s.re.test(m.name));
      m.userData.aikoOrig ??= { color: m.color.clone(), shade: m.shadeColorFactor?.clone() };
      const c = new THREE.Color(outfit[slot.key]);
      m.color.copy(c);
      if (m.shadeColorFactor) m.shadeColorFactor.copy(c).multiplyScalar(0.72);
    }
  });
}

/** Где у модели голова: верх, лицо, глаза — в мировых координатах. */
function measureHead(vrm) {
  const headPos = vrm.humanoid.getRawBoneNode("head").getWorldPosition(new THREE.Vector3());
  const face = findByName(vrm.scene, /^face$/i);
  const hair = findByName(vrm.scene, /^hair$/i);
  const faceBox = face ? new THREE.Box3().setFromObject(face) : null;

  const eyes = ["leftEye", "rightEye"]
    .map((b) => vrm.humanoid.getRawBoneNode(b)?.getWorldPosition(new THREE.Vector3()))
    .filter(Boolean);
  const eyeY = eyes.length ? eyes[0].y : headPos.y + 0.07;
  const halfWidth = faceBox ? (faceBox.max.x - faceBox.min.x) / 2 : 0.08;
  const front = faceBox ? faceBox.max.z : headPos.z + 0.09;
  const top = hair ? new THREE.Box3().setFromObject(hair).max.y : eyeY + 0.1;

  return {
    x: headPos.x,
    eyeY,
    eyeX: eyes.length === 2 ? Math.abs(eyes[0].x - eyes[1].x) / 2 : 0.032,
    front,
    top,
    r: halfWidth,
    centerZ: front - halfWidth * 1.05,
  };
}

function findByName(root, re) {
  let found = null;
  root.traverse((o) => { if (!found && re.test(o.name)) found = o; });
  return found;
}

const toon = (color) => new THREE.MeshToonMaterial({ color });

function buildAccessory(kind, h, color) {
  const g = new THREE.Group();
  g.position.set(h.x, 0, 0);
  const add = (geo, mat, x, y, z, rx = 0, ry = 0, rz = 0) => {
    const m = new THREE.Mesh(geo, mat);
    m.position.set(x, y, z);
    m.rotation.set(rx, ry, rz);
    g.add(m);
    return m;
  };
  const R = h.r * 1.12; // голова вместе с волосами

  switch (kind) {
    case "glasses": {
      const mat = toon(color);
      // кости глаз у VRoid стоят ближе к носу, чем нарисованные зрачки — считаем от ширины лица
      const dx = h.r * 0.45;
      const ring = h.r * 0.3;
      const y = h.eyeY - 0.006;
      const z = h.front - 0.012;
      for (const s of [-1, 1]) add(new THREE.TorusGeometry(ring, 0.0028, 6, 24), mat, s * dx, y, z);
      add(new THREE.CylinderGeometry(0.0022, 0.0022, dx * 2 - ring * 2, 6), mat, 0, y + 0.004, z, 0, 0, Math.PI / 2);
      break;
    }
    case "cat_ears": {
      const outer = toon(color);
      const inner = toon(new THREE.Color("#ffc2d9"));
      for (const s of [-1, 1]) {
        add(new THREE.ConeGeometry(R * 0.38, R * 0.62, 12), outer, s * R * 0.58, h.top - R * 0.05, h.centerZ, 0, 0, -s * 0.38);
        add(new THREE.ConeGeometry(R * 0.24, R * 0.42, 12), inner, s * R * 0.56, h.top - R * 0.08, h.centerZ + R * 0.12, 0, 0, -s * 0.38);
      }
      break;
    }
    case "cap":
    case "hard_hat": {
      const mat = toon(color);
      const dome = add(new THREE.SphereGeometry(R * 1.02, 28, 14, 0, Math.PI * 2, 0, Math.PI / 2), mat,
        0, h.top - R * (kind === "cap" ? 0.85 : 0.62), h.centerZ);
      dome.scale.y = 0.9;
      if (kind === "cap") {
        // козырёк — передняя половина диска
        add(new THREE.CylinderGeometry(R * 0.85, R * 0.85, 0.008, 24, 1, false, -Math.PI / 2, Math.PI), mat,
          0, h.top - R * 0.83, h.centerZ + R * 0.55, 0.12);
      } else {
        add(new THREE.CylinderGeometry(R * 1.28, R * 1.28, 0.01, 32), mat, 0, h.top - R * 0.62, h.centerZ);
        add(new THREE.TorusGeometry(R * 0.95, 0.008, 6, 24, Math.PI), mat, 0, h.top - R * 0.6, h.centerZ, 0, Math.PI / 2, 0);
      }
      break;
    }
    case "beret": {
      const b = add(new THREE.SphereGeometry(R * 1.12, 28, 14), toon(color), R * 0.12, h.top - R * 0.3, h.centerZ, 0, 0, 0.22);
      b.scale.set(1, 0.32, 1);
      add(new THREE.CylinderGeometry(0.004, 0.004, 0.018, 6), toon(color), R * 0.1, h.top + R * 0.02, h.centerZ, 0, 0, 0.22);
      break;
    }
    case "headphones": {
      const mat = toon(color);
      const dark = toon(new THREE.Color("#2b2233"));
      const earY = h.eyeY - 0.02;
      const earZ = h.centerZ - R * 0.1;
      const span = R * 1.08;
      add(new THREE.TorusGeometry(span, 0.008, 8, 32, Math.PI), mat, 0, earY, earZ);
      g.children[0].scale.y = (h.top + 0.012 - earY) / span;
      for (const s of [-1, 1]) {
        add(new THREE.CylinderGeometry(R * 0.36, R * 0.36, R * 0.28, 20), mat, s * span, earY, earZ, 0, 0, Math.PI / 2);
        add(new THREE.CylinderGeometry(R * 0.3, R * 0.3, R * 0.1, 20), dark, s * (span - R * 0.16), earY, earZ, 0, 0, Math.PI / 2);
      }
      break;
    }
    case "bow": {
      const mat = toon(color);
      const x = R * 0.55;
      const y = h.top - R * 0.15;
      add(new THREE.SphereGeometry(R * 0.11, 12, 8), mat, x, y, h.centerZ + R * 0.2);
      for (const s of [-1, 1]) {
        add(new THREE.ConeGeometry(R * 0.2, R * 0.38, 4), mat, x + s * R * 0.2, y, h.centerZ + R * 0.2, 0, 0, s * Math.PI / 2);
      }
      break;
    }
    case "witch_hat": {
      const mat = toon(color);
      const y = h.top - R * 0.55;
      add(new THREE.CylinderGeometry(R * 1.5, R * 1.5, 0.008, 32), mat, 0, y, h.centerZ, 0.08, 0, 0);
      // конус чуть заломлен назад, как у настоящей ведьмы
      add(new THREE.ConeGeometry(R * 0.78, R * 1.7, 24), mat, 0, y + R * 0.82, h.centerZ - R * 0.12, -0.22, 0, 0.12);
      add(new THREE.CylinderGeometry(R * 0.8, R * 0.82, R * 0.16, 24), toon(new THREE.Color("#ffb347")), 0, y + R * 0.1, h.centerZ);
      break;
    }
    case "santa_hat": {
      const white = toon(new THREE.Color("#ffffff"));
      const y = h.top - R * 0.5;
      add(new THREE.ConeGeometry(R * 0.98, R * 1.3, 24), toon(color), 0, y + R * 0.6, h.centerZ - R * 0.15, -0.45, 0, 0);
      add(new THREE.TorusGeometry(R * 0.98, R * 0.13, 10, 28), white, 0, y, h.centerZ, Math.PI / 2 - 0.08, 0, 0);
      add(new THREE.SphereGeometry(R * 0.17, 12, 8), white, 0, y + R * 1.1, h.centerZ - R * 0.75);
      break;
    }
    default:
      return null;
  }
  return g;
}

/** Значок на груди: круг с текстом, прилипает к кофте через raycast. */
function buildBadge(vrm, outfit) {
  const chest = vrm.humanoid.getRawBoneNode("upperChest") || vrm.humanoid.getRawBoneNode("chest");
  if (!chest) return null;
  const p = chest.getWorldPosition(new THREE.Vector3());

  // ищем поверхность кофты перед грудью (сбоку, как приколотый пин)
  const ray = new THREE.Raycaster(new THREE.Vector3(p.x + 0.055, p.y + 0.02, p.z + 1), new THREE.Vector3(0, 0, -1));
  const hit = ray.intersectObject(vrm.scene, true)
    .find((i) => /cloth/i.test([].concat(i.object.material).map((m) => m.name).join(" ")));
  if (!hit) return null;

  const size = 128;
  const cnv = document.createElement("canvas");
  cnv.width = cnv.height = size;
  const ctx = cnv.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = outfit.badge_color;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 10, 0, Math.PI * 2);
  ctx.fill();
  const text = outfit.badge_text;
  ctx.fillStyle = new THREE.Color(outfit.badge_color).getHSL({}).l > 0.6 ? "#2b2233" : "#ffffff";
  ctx.font = `900 ${text.length > 2 ? 40 : 54}px system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, size / 2, size / 2 + 3);

  const tex = new THREE.CanvasTexture(cnv);
  tex.colorSpace = THREE.SRGBColorSpace;
  const badge = new THREE.Mesh(
    new THREE.CircleGeometry(0.024, 32),
    new THREE.MeshBasicMaterial({ map: tex, transparent: true }),
  );
  badge.position.copy(hit.point).add(new THREE.Vector3(0, 0, 0.006));
  badge.rotation.x = -0.25; // грудь чуть смотрит вверх
  chest.attach(badge);
  return badge;
}
