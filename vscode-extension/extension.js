// Расширение VS Code: Айко в боковой панели (страница server.py во фрейме),
// команды, которые отправляют ей выделенный код или текущий файл,
// и живой контекст: какой файл открыт и что выделено — чтобы она понимала «вот это».
const http = require("http");
const vscode = require("vscode");

const MAX_CHARS = 15000; // столько кода за раз ей хватит, больше — только тратить токены
const MAX_CONTEXT_CHARS = 4000; // выделение для контекста — серверу больше и не нужно
const CONTEXT_DEBOUNCE_MS = 800;

/** @type {vscode.WebviewView | undefined} */
let view;

function serverUrl() {
  return vscode.workspace.getConfiguration("aiko").get("serverUrl", "http://127.0.0.1:8765").replace(/\/+$/, "");
}

function html() {
  const url = serverUrl();
  const nonce = Math.random().toString(36).slice(2);
  // фрейм с локальным сервером + маленький скрипт, который пересылает ему сообщения расширения
  return `<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${url}; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>html,body,iframe{margin:0;padding:0;width:100%;height:100%;border:0;overflow:hidden}</style></head>
<body>
<iframe id="aiko" src="${url}/?embed=vscode" allow="microphone; autoplay"></iframe>
<script nonce="${nonce}">
  const frame = document.getElementById("aiko");
  window.addEventListener("message", (e) => {
    if (e.data && e.data.type === "aiko-ask") frame.contentWindow.postMessage(e.data, ${JSON.stringify(url)});
  });
</script>
</body></html>`;
}

function relativePath(doc) {
  return vscode.workspace.asRelativePath(doc.uri, false);
}

async function send(message) {
  if (!view) {
    await vscode.commands.executeCommand("aiko.panel.focus");
    await new Promise((r) => setTimeout(r, 1500)); // даём фрейму загрузиться
  }
  view?.show?.(true);
  view?.webview.postMessage({ type: "aiko-ask", ...message });
}

/** Сообщаем серверу, что сейчас на экране у семпая. Сервер не запущен — молча пропускаем. */
function postContext(body) {
  let url;
  try { url = new URL("/api/context", serverUrl()); } catch { return; }
  const data = JSON.stringify(body);
  const req = http.request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) },
    timeout: 3000,
  });
  req.on("error", () => {});
  req.on("timeout", () => req.destroy());
  req.end(data);
}

let contextTimer;
let lastContext = "";
let lastSentAt = 0;
const CONTEXT_RESEND_MS = 5 * 60_000; // повторяем даже без изменений: сервер могли перезапустить
function shareEditor() {
  clearTimeout(contextTimer);
  contextTimer = setTimeout(() => {
    if (!vscode.workspace.getConfiguration("aiko").get("shareEditor", true)) return;
    const editor = vscode.window.activeTextEditor;
    const doc = editor?.document;
    // только файлы на диске: безымянные и служебные (вывод, настройки) не отправляем
    const body = doc && doc.uri.scheme === "file" ? {
      path: doc.uri.fsPath,
      line: editor.selection.start.line + 1,
      end_line: editor.selection.end.line + 1,
      selection: editor.selection.isEmpty ? "" : doc.getText(editor.selection).slice(0, MAX_CONTEXT_CHARS),
    } : { path: null };
    const key = JSON.stringify(body);
    if (key === lastContext && Date.now() - lastSentAt < CONTEXT_RESEND_MS) return;
    lastContext = key;
    lastSentAt = Date.now();
    postContext(body);
  }, CONTEXT_DEBOUNCE_MS);
}

function activate(context) {
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("aiko.panel", {
      resolveWebviewView(webviewView) {
        view = webviewView;
        webviewView.webview.options = { enableScripts: true };
        webviewView.webview.html = html();
        webviewView.onDidDispose(() => { view = undefined; });
      },
    }, { webviewOptions: { retainContextWhenHidden: true } }),

    vscode.commands.registerCommand("aiko.askSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.selection.isEmpty) {
        vscode.window.showInformationMessage("Выдели код, о котором спросить Айко.");
        return;
      }
      const question = await vscode.window.showInputBox({
        prompt: "Что спросить про этот код?",
        value: "Объясни, что здесь происходит",
      });
      if (question === undefined) return;
      const sel = editor.selection;
      const where = `${relativePath(editor.document)}:${sel.start.line + 1}-${sel.end.line + 1}`;
      const code = editor.document.getText(sel).slice(0, MAX_CHARS);
      await send({
        label: `💻 ${question} — \`${where}\``,
        text: `Семпай в VS Code выделил код из \`${where}\` и спрашивает: ${question}\n\n\`\`\`\n${code}\n\`\`\``,
      });
    }),

    vscode.commands.registerCommand("aiko.explainFile", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const path = relativePath(editor.document);
      await send({
        label: `💻 Объясни файл \`${path}\``,
        text: `Семпай открыл в VS Code файл \`${path}\`. Объясни, зачем он нужен и как устроен; ` +
              `при необходимости прочитай его инструментами и покажи главное через show_code.`,
      });
    }),

    vscode.commands.registerCommand("aiko.reload", () => {
      if (view) view.webview.html = html();
      lastSentAt = 0; // сервер могли перезапустить — расскажем заново
      shareEditor();
    }),

    vscode.window.onDidChangeActiveTextEditor(shareEditor),
    vscode.window.onDidChangeTextEditorSelection(shareEditor),
    { dispose: () => clearTimeout(contextTimer) },
  );
  shareEditor();
}

function deactivate() {}

module.exports = { activate, deactivate };
