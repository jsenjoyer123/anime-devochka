// «Экран стрима» рядом с Айко: фрагменты кода и Mermaid-схемы.
// Хранит несколько последних показов, чтобы можно было пролистать назад.
// Mermaid тяжёлый (~2 МБ), поэтому грузим его только при первой схеме.

const MAX_ITEMS = 6;

const screen = document.getElementById("screen");
const titleEl = document.getElementById("screen-title");
const body = document.getElementById("screen-body");
const prevBtn = document.getElementById("screen-prev");
const nextBtn = document.getElementById("screen-next");

let items = [];
let index = -1;
let mermaidPromise = null;
let diagramSeq = 0;

document.getElementById("screen-close").onclick = hideScreen;
prevBtn.onclick = () => go(index - 1);
nextBtn.onclick = () => go(index + 1);

export const isScreenVisible = () => !screen.hidden;

export function hideScreen() {
  screen.hidden = true;
  body.replaceChildren();
}

export function showCode({ path, start, code, caption }) {
  push({ kind: "code", title: `${path}:${start}${caption ? ` — ${caption}` : ""}`, start, code });
}

export function showDiagram({ title, mermaid }) {
  push({ kind: "diagram", title: title || "Схема", source: mermaid });
}

function push(item) {
  items.push(item);
  if (items.length > MAX_ITEMS) items.shift();
  go(items.length - 1);
}

function go(i) {
  if (i < 0 || i >= items.length) return;
  index = i;
  const item = items[i];
  titleEl.textContent = item.title;
  prevBtn.disabled = i === 0;
  nextBtn.disabled = i === items.length - 1;
  screen.hidden = false;
  if (item.kind === "code") renderCode(item);
  else renderDiagram(item);
}

function renderCode({ start, code }) {
  const pre = document.createElement("pre");
  pre.style.setProperty("--start", String(start - 1));
  for (const line of code.split("\n")) {
    const span = document.createElement("span");
    span.textContent = line || " ";
    pre.append(span);
  }
  body.replaceChildren(pre);
}

async function renderDiagram(item) {
  const box = document.createElement("div");
  box.className = "diagram";
  box.textContent = "рисую…";
  body.replaceChildren(box);
  try {
    mermaidPromise ??= import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs").then(({ default: m }) => {
      // strict: никаких скриптов и ссылок из текста схемы
      m.initialize({ startOnLoad: false, securityLevel: "strict", theme: "base",
        themeVariables: { primaryColor: "#ffe1ef", primaryBorderColor: "#e2336b", lineColor: "#b03a7c" } });
      return m;
    });
    const mermaid = await mermaidPromise;
    item.svg ??= (await mermaid.render(`aiko-diagram-${++diagramSeq}`, item.source)).svg;
    box.innerHTML = item.svg; // svg уже очищен mermaid (DOMPurify в strict-режиме)
  } catch (e) {
    const pre = document.createElement("pre");
    pre.textContent = item.source;
    box.replaceChildren(`Не получилось нарисовать (${e.message}). Вот исходник:`, pre);
  }
}
