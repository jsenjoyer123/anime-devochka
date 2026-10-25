// Тест расширения VS Code без VS Code: модуль vscode подменён заглушкой, сервер Айко — фейковый.
// Запуск:  node --test tests/vscode
const test = require("node:test");
const assert = require("node:assert");
const http = require("node:http");
const Module = require("node:module");
const path = require("node:path");

// --- заглушка vscode: только то, что использует расширение ---
const handlers = {};
const diagnostics = new Map();
let editor;
let serverUrl;
const vscode = {
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2 },
  Range: class { constructor(l1, c1, l2, c2) { Object.assign(this, { l1, c1, l2, c2 }); } },
  Diagnostic: class { constructor(range, message, severity) { Object.assign(this, { range, message, severity }); } },
  Uri: { file: (p) => ({ fsPath: p, scheme: "file" }) },
  languages: {
    createDiagnosticCollection: () => ({
      clear: () => diagnostics.clear(),
      set: (uri, list) => diagnostics.set(uri.fsPath, list),
      dispose() {},
    }),
  },
  workspace: {
    getConfiguration: () => ({ get: (key, def) => (key === "serverUrl" ? serverUrl : def) }),
    asRelativePath: (uri) => uri.fsPath,
  },
  window: {
    get activeTextEditor() { return editor; },
    registerWebviewViewProvider: () => ({ dispose() {} }),
    onDidChangeActiveTextEditor: (fn) => { handlers.active = fn; return { dispose() {} }; },
    onDidChangeTextEditorSelection: (fn) => { handlers.selection = fn; return { dispose() {} }; },
    showInformationMessage() {},
  },
  commands: { registerCommand: (name, fn) => { handlers[name] = fn; return { dispose() {} }; } },
};
const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  return request === "vscode" ? "vscode" : resolve.call(this, request, ...rest);
};
require.cache.vscode = { id: "vscode", filename: "vscode", loaded: true, exports: vscode };

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

test("контекст редактора и замечания ревью", async (t) => {
  const posts = [];
  let annotations = { root: "/proj", version: 1, notes: [
    { path: "src/app.py", line: 3, severity: "error", message: "тут баг" },
    { path: "src/app.py", line: 7, severity: "info", message: "мелочь" },
  ] };
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => { body += c; });
    req.on("end", () => {
      if (req.method === "POST") posts.push({ url: req.url, type: req.headers["content-type"], body: JSON.parse(body) });
      if (req.method === "POST" && req.url === "/api/annotations") annotations = { ...annotations, version: 2, notes: [] };
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(req.url === "/api/annotations" ? annotations : { ok: true }));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  serverUrl = `http://127.0.0.1:${server.address().port}`;
  t.after(() => server.close());

  const doc = { uri: { fsPath: "/proj/src/app.py", scheme: "file" }, getText: () => "x = 1" };
  editor = { document: doc, selection: { start: { line: 4 }, end: { line: 5 }, isEmpty: false } };
  const subscriptions = [];
  const ext = require(path.join(__dirname, "../../vscode-extension/extension.js"));
  ext.activate({ subscriptions });
  t.after(() => subscriptions.forEach((s) => s.dispose()));

  await wait(1200); // дебаунс контекста
  const ctx = posts.find((p) => p.url === "/api/context");
  assert.equal(ctx.type, "application/json");
  assert.deepEqual(ctx.body, { path: "/proj/src/app.py", line: 5, end_line: 6, selection: "x = 1" });

  const list = diagnostics.get("/proj/src/app.py");
  assert.deepEqual(list.map((d) => [d.range.l1, d.severity, d.message]), [[2, 0, "тут баг"], [6, 2, "мелочь"]]);

  // то же самое выделение второй раз не шлём
  handlers.selection();
  await wait(1000);
  assert.equal(posts.filter((p) => p.url === "/api/context").length, 1);

  // «убрать замечания»: чистим у себя и просим сервер забыть
  handlers["aiko.clearAnnotations"]();
  await wait(200);
  assert.equal(diagnostics.size, 0);
  assert.ok(posts.some((p) => p.url === "/api/annotations"));
});
