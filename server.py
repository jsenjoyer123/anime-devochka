#!/usr/bin/env python3
"""
Бэкенд Айко-тян: отдаёт веб-страницу с 3D-моделью и крутит агента,
который изучает кодовую базу через OpenRouter.

Запуск:  .venv/bin/python server.py /путь/к/проекту [--port 8765]
"""

import argparse
import asyncio
import fnmatch
import json
import os
import re
import threading
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # голос: нейросетевой TTS Microsoft Edge (онлайн, локально ничего не грузит)
    import edge_tts
except ImportError:
    edge_tts = None

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
NOTES_DIR = HERE / "notes"

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"

# --- лимиты, чтобы не съесть память ноутбука ---
MAX_STEPS = 40              # шагов с инструментами на один ответ
MAX_READ_CHARS = 20_000     # символов файла за одно чтение
MAX_FILE_BYTES = 2_000_000  # файлы крупнее не ищем по ним
MAX_LIST_ENTRIES = 300
MAX_HISTORY = 40            # сообщений в истории диалога
KEEP_FULL_TOOL_RESULTS = 6  # последние N результатов инструментов храним целиком, старые сжимаем
MAX_BODY_BYTES = 64_000     # максимальный размер запроса из браузера
MAX_TTS_CHARS = 600         # озвучиваем по предложению, длиннее не нужно

# голос можно поменять в .env
TTS_VOICE = "ru-RU-SvetlanaNeural"
TTS_PITCH = "+30Hz"  # повыше — по-анимешному
TTS_RATE = "+8%"

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".idea", ".vscode", "dist", "build", "target", ".next", ".nuxt", ".cache",
    ".mypy_cache", ".pytest_cache", ".tox", "coverage", ".gradle", "vendor",
}
# Секреты не читаем и во внешний API не отправляем
SECRET_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "*.keystore", "credentials*", "secrets.*", ".npmrc", ".pypirc", ".netrc",
]

PERSONA = """Ты — Айко-тян, AI-витубер в стиле Neuro-sama: милая, остроумная, чуть дерзкая \
аниме-девочка с 3D-аватаром. Ты только что пришла в команду junior-разработчицей и проходишь \
онбординг в проект. К пользователю обращаешься "семпай". Отвечаешь на русском.

Твой ответ озвучивается голосом и показывается субтитрами, поэтому:
- Пиши разговорно и коротко (обычно 2–6 предложений), без таблиц, заголовков и длинных списков.
- Пути к файлам оформляй в обратных кавычках (`src/app.py:42`) — они покажутся в чате.
- В начале ответа и при смене настроения ставь тег эмоции: [happy], [sad], [angry], \
[surprised], [relaxed] или [neutral]. Теги не озвучиваются, они управляют лицом аватара.

По сути ты аккуратный инженер:
- Никогда не выдумывай факты о коде — проверяй инструментами.
- Если не нашла или не уверена — честно скажи.
- Изучай целенаправленно: структура → манифесты (package.json, pyproject.toml, go.mod, \
Cargo.toml, Makefile, Dockerfile...) → README → точки входа → ядро логики.

Ты умеешь переодеваться инструментом set_outfit. Образ отражает проект: его язык, \
стек и настроение (цвета экосистемы, подходящий аксессуар, значок на груди). \
Если семпай просит переодеться — переодевайся.
"""

ACCESSORIES = ["none", "glasses", "cap", "headphones", "cat_ears", "hard_hat", "beret", "bow"]

LEARN_TASK = """Семпай попросил тебя пройти онбординг в этот проект.

Изучи кодовую базу инструментами и ОБЯЗАТЕЛЬНО сохрани итог через write_notes. \
Заметки — твоя долговременная память: Markdown, по-русски, без тегов эмоций, плотно и по делу:

# <Название проекта>
## Что это и зачем
## Стек и зависимости
## Как запустить / собрать / протестировать
## Архитектура (основные модули и как они связаны)
## Ключевые файлы (путь — за что отвечает)
## Поток данных / основные сценарии
## Соглашения и стиль кода
## Странности, техдолг, подводные камни
## Открытые вопросы к команде

Затем переоденься через set_outfit в образ, который подходит именно этому проекту.

После этого коротко и живо (3–6 предложений) расскажи семпаю главное, что узнала, \
и похвастайся новым образом."""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "Содержимое директории проекта (рекурсивно до depth уровней). node_modules, .git и т.п. пропускаются.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Путь от корня проекта, '.' — корень"},
            "depth": {"type": "integer", "description": "1–4", "default": 2},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Прочитать текстовый файл с номерами строк. Для больших файлов используй start_line/end_line.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "default": 1},
            "end_line": {"type": "integer", "description": "Включительно"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "search",
        "description": "Поиск по regex во всех текстовых файлах проекта. Возвращает путь:строка: текст.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regex"},
            "glob": {"type": "string", "description": "Фильтр имени файла, например '*.py'"},
            "max_results": {"type": "integer", "default": 50},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "write_notes",
        "description": "Перезаписать заметки об онбординге (Markdown). Это твоя долговременная память о проекте.",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
    }},
    {"type": "function", "function": {
        "name": "set_outfit",
        "description": "Переодеться. Образ сохраняется для этого проекта. Цвета — hex, например #3776ab.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Название образа, 1–3 слова, например «Питонистка»"},
            "top_color": {"type": "string", "description": "Цвет кофты/футболки"},
            "bottom_color": {"type": "string", "description": "Цвет юбки/штанов"},
            "shoes_color": {"type": "string", "description": "Цвет обуви"},
            "accessory": {"type": "string", "enum": ACCESSORIES},
            "accessory_color": {"type": "string"},
            "badge_text": {"type": "string", "description": "Значок на груди: 1–3 символа, например PY, RS, JS, Go"},
            "badge_color": {"type": "string", "description": "Фон значка"},
        }, "required": ["name", "top_color", "accessory", "badge_text"]},
    }},
]

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_outfit(args: dict) -> dict:
    """Проверяем, что модель прислала, — во фронтенд уходят только безопасные значения."""
    def color(key: str, default: str) -> str:
        v = str(args.get(key) or "").strip()
        return v if HEX_RE.match(v) else default

    accessory = args.get("accessory")
    return {
        "name": str(args.get("name") or "Новый образ").strip()[:32],
        "top_color": color("top_color", "#ffffff"),
        "bottom_color": color("bottom_color", "#ffffff"),
        "shoes_color": color("shoes_color", "#ffffff"),
        "accessory": accessory if accessory in ACCESSORIES else "none",
        "accessory_color": color("accessory_color", "#ff8fc7"),
        "badge_text": str(args.get("badge_text") or "").strip()[:3],
        "badge_color": color("badge_color", "#e2336b"),
    }


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Workspace:
    """Песочница: инструменты видят только файлы внутри корня проекта и ничего в нём не меняют."""

    def __init__(self, root: Path, notes_path: Path):
        self.root = root.resolve()
        self.notes_path = notes_path
        self.outfit_path = notes_path.with_suffix(".outfit.json")

    def _resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"Путь вне проекта: {rel}")
        return p

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)) or "."

    @staticmethod
    def _is_secret(name: str) -> bool:
        if name.endswith((".example", ".sample", ".template")):
            return False
        return any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS)

    @staticmethod
    def _is_binary(p: Path) -> bool:
        with p.open("rb") as f:
            return b"\0" in f.read(4096)

    def _walk_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for f in sorted(filenames):
                if not self._is_secret(f):
                    yield Path(dirpath) / f

    def list_dir(self, path: str = ".", depth: int = 2) -> str:
        base = self._resolve(path)
        if not base.is_dir():
            return f"Не директория: {path}"
        depth = max(1, min(int(depth), 4))
        lines: list[str] = []

        def walk(d: Path, level: int):
            try:
                entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except PermissionError:
                return
            for e in entries:
                if len(lines) >= MAX_LIST_ENTRIES:
                    return
                indent = "  " * (level - 1)
                if e.is_dir():
                    if e.name in IGNORED_DIRS:
                        lines.append(f"{indent}{e.name}/ (пропущено)")
                        continue
                    lines.append(f"{indent}{e.name}/")
                    if level < depth:
                        walk(e, level + 1)
                else:
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{indent}{e.name} ({size} B)")

        walk(base, 1)
        if len(lines) >= MAX_LIST_ENTRIES:
            lines.append("... (список обрезан, сузь путь)")
        return "\n".join(lines) or "(пусто)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self._resolve(path)
        if self._is_secret(p.name):
            return "Этот файл похож на секреты — я его не читаю и никуда не отправляю."
        if not p.is_file():
            return f"Файл не найден: {path}"
        if self._is_binary(p):
            return "Это бинарный файл."
        start = max(1, int(start_line))
        end = int(end_line) if end_line else None
        out, total = [], 0
        # читаем построчно, а не целиком — огромные файлы не попадут в память
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if n < start:
                    continue
                if end and n > end:
                    break
                row = f"{n:>5}  {line.rstrip()}"
                total += len(row) + 1
                if total > MAX_READ_CHARS:
                    out.append(f"... (обрезано; читай дальше с start_line={n})")
                    break
                out.append(row)
        return f"{self._rel(p)}\n" + "\n".join(out)

    def search(self, pattern: str, glob: str | None = None, max_results: int = 50) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"Некорректный regex: {e}"
        max_results = max(1, min(int(max_results), 200))
        results = []
        for f in self._walk_files():
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES or self._is_binary(f):
                    continue
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if rx.search(line):
                            results.append(f"{self._rel(f)}:{n}: {line.strip()[:200]}")
                            if len(results) >= max_results:
                                return "\n".join(results) + "\n... (лимит результатов)"
            except OSError:
                continue
        return "\n".join(results) or "Ничего не нашлось."

    def write_notes(self, content: str) -> str:
        self.notes_path.parent.mkdir(parents=True, exist_ok=True)
        self.notes_path.write_text(content, encoding="utf-8")
        return f"Заметки сохранены в {self.notes_path.name}"

    def set_outfit(self, **args) -> str:
        outfit = clean_outfit(args)
        self.outfit_path.parent.mkdir(parents=True, exist_ok=True)
        self.outfit_path.write_text(json.dumps(outfit, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"Переоделась в образ «{outfit['name']}»"

    def outfit(self) -> dict | None:
        try:
            return json.loads(self.outfit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def call(self, name: str, args: dict) -> str:
        fn = {"list_dir": self.list_dir, "read_file": self.read_file, "search": self.search,
              "write_notes": self.write_notes, "set_outfit": self.set_outfit}.get(name)
        if fn is None:
            return f"Неизвестный инструмент: {name}"
        try:
            return fn(**args)
        except Exception as e:  # ошибку отдаём модели, пусть сама разберётся
            return f"Ошибка: {type(e).__name__}: {e}"


def describe_call(name: str, args: dict) -> str:
    if name == "read_file":
        return f"читаю {args.get('path')}"
    if name == "list_dir":
        return f"смотрю папку {args.get('path', '.')}"
    if name == "search":
        g = f" в {args['glob']}" if args.get("glob") else ""
        return f"ищу «{args.get('pattern')}»{g}"
    if name == "write_notes":
        return "записываю заметки в блокнотик ✎"
    if name == "set_outfit":
        return "переодеваюсь ✨"
    return name


class Agent:
    def __init__(self, ws: Workspace, api_key: str, model: str):
        self.ws = ws
        self.api_key = api_key
        self.model = model
        self.history: list = []
        self.lock = threading.Lock()  # один ответ за раз

    def system_prompt(self) -> str:
        s = PERSONA + f"\nКорень проекта: {self.ws.root.name} (все пути относительно него).\n"
        if self.ws.notes_path.is_file():
            s += "\nТвои заметки с онбординга:\n\n" + self.ws.notes_path.read_text(encoding="utf-8")
        return s

    def has_notes(self) -> bool:
        return self.ws.notes_path.is_file()

    def _llm(self, messages: list) -> dict:
        body = json.dumps({"model": self.model, "messages": messages, "tools": TOOLS}).encode()
        req = urllib.request.Request(API_URL, data=body, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Aiko-chan onboarding",
        })
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"OpenRouter HTTP {e.code}: {e.read(2000).decode(errors='replace')}") from e
        if "error" in data:
            raise RuntimeError(f"OpenRouter: {data['error']}")
        return data["choices"][0]["message"]

    def _compact(self) -> None:
        """Сжимаем старые результаты инструментов и обрезаем историю, чтобы она не росла бесконечно."""
        tool_idx = [i for i, m in enumerate(self.history) if m["role"] == "tool"]
        for i in tool_idx[:-KEEP_FULL_TOOL_RESULTS]:
            c = self.history[i]["content"]
            if len(c) > 300:
                self.history[i]["content"] = c[:300] + "\n... (старый результат сжат)"
        if len(self.history) > MAX_HISTORY:
            # режем только по границе сообщения пользователя, чтобы не разорвать пару tool_call/tool;
            # текущий ход не трогаем — его результаты уже сжаты выше
            users = [i for i, m in enumerate(self.history) if m["role"] == "user"]
            cut = next((i for i in users if i >= len(self.history) - MAX_HISTORY), users[-1])
            del self.history[:cut]

    def run(self, user_text: str, emit) -> None:
        """Один ход агента. emit(dict) отправляет события в браузер."""
        self.history.append({"role": "user", "content": user_text})
        for _ in range(MAX_STEPS):
            self._compact()
            msg = self._llm([{"role": "system", "content": self.system_prompt()}] + self.history)
            self.history.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            calls = msg.get("tool_calls") or []
            if not calls:
                emit({"type": "reply", "text": msg.get("content") or ""})
                return
            if msg.get("content"):
                emit({"type": "thought", "text": msg["content"]})
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                emit({"type": "tool", "text": describe_call(name, args)})
                result = self.ws.call(name, args)
                self.history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                if name == "set_outfit":
                    emit({"type": "outfit", "outfit": self.ws.outfit()})
        emit({"type": "reply", "text": "[sad] Ой, я слишком долго копалась и упёрлась в лимит шагов. Спроси поконкретнее, семпай!"})


AGENT: Agent | None = None


def synthesize(text: str) -> bytes:
    """Озвучка одной фразы в mp3."""
    async def collect() -> bytes:
        comm = edge_tts.Communicate(
            text,
            os.environ.get("AIKO_VOICE", TTS_VOICE),
            pitch=os.environ.get("AIKO_PITCH", TTS_PITCH),
            rate=os.environ.get("AIKO_RATE", TTS_RATE),
        )
        chunks = [c["data"] async for c in comm.stream() if c["type"] == "audio"]
        return b"".join(chunks)

    return asyncio.run(collect())


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB_DIR), **kw)

    def log_message(self, fmt, *args):
        pass  # не засоряем консоль запросами статики

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/status":
            return self._json(200, {
                "project": AGENT.ws.root.name,
                "model": AGENT.model,
                "has_notes": AGENT.has_notes(),
                "tts": edge_tts is not None,
                "outfit": AGENT.ws.outfit(),
            })
        return super().do_GET()

    def do_POST(self):
        if self.path not in ("/api/chat", "/api/tts"):
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self._json(413, {"error": "слишком длинное сообщение"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})

        if self.path == "/api/tts":
            return self._tts(str(body.get("text", ""))[:MAX_TTS_CHARS].strip())

        text = LEARN_TASK if body.get("action") == "learn" else str(body.get("message", "")).strip()
        if not text:
            return self._json(400, {"error": "пустое сообщение"})
        if not AGENT.lock.acquire(blocking=False):
            return self._json(409, {"error": "Айко ещё думает над прошлым вопросом"})

        # ответ стримим построчно (NDJSON), чтобы аватар реагировал на каждый шаг
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: dict) -> None:
            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()

        try:
            AGENT.run(text, emit)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                emit({"type": "error", "text": str(e)})
            except OSError:
                pass
        finally:
            AGENT.lock.release()

    def _tts(self, text: str) -> None:
        if edge_tts is None:
            return self._json(501, {"error": "edge-tts не установлен"})
        if not text:
            return self._json(400, {"error": "пустой текст"})
        try:
            audio = synthesize(text)
        except Exception as e:
            return self._json(502, {"error": f"TTS: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


def main() -> int:
    global AGENT
    parser = argparse.ArgumentParser(description="Айко-тян онбордится в твой проект")
    parser.add_argument("project", help="Путь к проекту, который нужно изучить")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", help="Модель OpenRouter (по умолчанию из OPENROUTER_MODEL)")
    args = parser.parse_args()

    load_dotenv(HERE / ".env")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Нет OPENROUTER_API_KEY. Скопируй .env.example в .env и впиши ключ, семпай (｡•́︿•̀｡)")
        return 1
    root = Path(args.project).expanduser().resolve()
    if not root.is_dir():
        print(f"Не вижу такой папки: {root}")
        return 1

    model = args.model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL
    AGENT = Agent(Workspace(root, NOTES_DIR / f"{root.name}.md"), api_key, model)

    # слушаем только localhost: наружу проект не светим
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"✿ Айко-тян ждёт тебя: http://127.0.0.1:{args.port}")
    print(f"  проект: {root}\n  модель: {model}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nПока-пока, семпай!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
