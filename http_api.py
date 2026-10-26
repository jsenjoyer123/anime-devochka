"""
HTTP-слой Айко: статика web/, API чата/голоса/проектов и ретрансляция в окна OBS.
Слушаем только localhost: сервер умеет запускать команды семпая.
"""

import json
import queue
import threading
import urllib.parse
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

import devtools
import usage
import workspace
from agent import Agent, TASKS, with_trace_task
from llm import MAX_AUDIO_BYTES, MAX_TTS_CHARS, edge_tts, synthesize, transcribe
from workspace import MAX_RECENT, Workspace

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY_BYTES = 64_000  # максимальный размер запроса из браузера

AGENT: Agent | None = None             # текущий проект
AGENTS: dict[Path, Agent] = {}          # открытые проекты: история диалога сохраняется при переключении
SWITCH_LOCK = threading.Lock()
API_KEY = ""
MODEL = ""


class Hub:
    """Ретрансляция событий основной вкладки в окна-зрители (OBS): субтитры, жесты, образ."""

    def __init__(self):
        self.subs: set[queue.Queue] = set()
        self.lock = threading.Lock()

    def publish(self, event: dict) -> None:
        with self.lock:
            for q in self.subs:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass  # зритель не успевает — лучше потерять кадр, чем копить память

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)


HUB = Hub()


def open_project(root: Path) -> Agent:
    """Сделать проект текущим (агента создаём один раз и держим в кэше)."""
    global AGENT
    with SWITCH_LOCK:
        if AGENT is not None and AGENT.lock.locked():
            raise RuntimeError("Айко сейчас занята ответом — переключимся, когда договорит")
        agent = AGENTS.get(root)
        if agent is None:
            agent = AGENTS[root] = Agent(Workspace(root, workspace.NOTES_DIR / f"{root.name}.md"),
                                         API_KEY, MODEL)
        AGENT = agent
        remember_recent(root)
        return agent


def recent_projects() -> list[str]:
    try:
        return [p for p in json.loads(workspace.RECENT_PATH.read_text(encoding="utf-8")) if Path(p).is_dir()]
    except (OSError, json.JSONDecodeError):
        return []


def remember_recent(root: Path) -> None:
    items = [str(root)] + [p for p in recent_projects() if p != str(root)]
    workspace.RECENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    workspace.RECENT_PATH.write_text(json.dumps(items[:MAX_RECENT], ensure_ascii=False, indent=2),
                                     encoding="utf-8")


def project_model_url(project: str) -> str | None:
    """Своя VRM-модель для проекта: web/models/projects/<проект>.vrm."""
    if (WEB_DIR / "models" / "projects" / f"{project}.vrm").is_file():
        return "models/projects/" + urllib.parse.quote(f"{project}.vrm")
    return None


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
        agent = AGENT
        if self.path == "/api/events":
            return self._events()
        if self.path == "/api/changes":
            return self._json(200, devtools.changes(agent.ws.root))
        if self.path == "/api/annotations":
            # расширение VS Code: замечания ревью для подчёркиваний; root — чтобы собрать полные пути
            return self._json(200, {"root": str(agent.ws.root), **agent.annotations})
        if self.path == "/api/status":
            return self._json(200, {
                "project": agent.ws.root.name,
                "path": str(agent.ws.root),
                "recent": recent_projects(),
                "model": agent.model,
                "has_notes": agent.has_notes(),
                "stale_commits": agent.ws.notes_staleness(),
                "skills": [{"id": k, "title": v["title"], "hint": v["hint"]} for k, v in agent.ws.skills().items()],
                "tts": edge_tts is not None,
                "outfit": agent.ws.outfit(),
                "has_doc": agent.ws.onboarding_path.is_file(),
                "model_url": project_model_url(agent.ws.root.name),
                "quest": agent.ws.quest.state(),
                "transcript": agent.transcript,
                "allowed": agent.ws.allowed_commands(),
                "spent_today": usage.today()["cost"],
            })
        if self.path == "/api/onboarding":
            return self._onboarding(agent)
        return super().do_GET()

    def _events(self) -> None:
        """SSE-поток для зрителей. Пинг раз в 15 секунд — так замечаем закрытые окна."""
        if not self._same_host():
            return self._json(403, {"error": "forbidden"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = HUB.subscribe()
        try:
            while True:
                try:
                    line = f"data: {json.dumps(q.get(timeout=15), ensure_ascii=False)}\n\n"
                except queue.Empty:
                    line = ": ping\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            HUB.unsubscribe(q)

    def _onboarding(self, agent: Agent) -> None:
        try:
            data = agent.ws.onboarding_path.read_bytes()
        except OSError:
            return self._json(404, {"error": "ONBOARDING.md ещё не написан"})
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="ONBOARDING.md"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _same_host(self) -> bool:
        return self.headers.get("Host") in self._allowed_hosts()

    def _same_origin(self) -> bool:
        """Защита от чужих сайтов в браузере (CSRF) и DNS rebinding: Айко умеет запускать команды."""
        allowed = self._allowed_hosts()
        if not self._same_host():
            return False
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] not in allowed:
            return False
        # JSON и audio/wav заставляют браузер делать preflight для чужих сайтов, а его мы не разрешаем
        ctype = self.headers.get("Content-Type") or ""
        return ctype.startswith("application/json") or ctype == "audio/wav"

    def do_POST(self):
        if self.path not in ("/api/chat", "/api/tts", "/api/confirm", "/api/stt", "/api/project", "/api/broadcast",
                             "/api/context", "/api/allowed", "/api/annotations"):
            return self._json(404, {"error": "not found"})
        if not self._same_origin():
            return self._json(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        if self.path == "/api/stt":
            return self._stt(length)
        if length > MAX_BODY_BYTES:
            return self._json(413, {"error": "слишком длинное сообщение"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})

        if self.path == "/api/tts":
            return self._tts(str(body.get("text", ""))[:MAX_TTS_CHARS].strip(), str(body.get("emotion") or ""))
        if self.path == "/api/broadcast":
            if isinstance(body.get("event"), dict):
                HUB.publish(body["event"])
            return self._json(200, {"ok": True, "viewers": len(HUB.subs)})

        agent = AGENT  # берём один раз: проект могут переключить посреди запроса
        if self.path == "/api/confirm":
            edited = body.get("message")
            ok = agent.resolve_confirm(str(body.get("id", "")), body.get("approve") is True,
                                       edited if isinstance(edited, str) else None, body.get("always") is True)
            return self._json(200 if ok else 404, {"ok": ok})
        if self.path == "/api/annotations":
            agent.set_annotations([])  # «убрать замечания» из VS Code
            return self._json(200, {"ok": True})
        if self.path == "/api/allowed":
            # отсюда можно только забрать разрешение: выдаётся оно лишь кнопкой при подтверждении
            agent.ws.set_allowed(str(body.get("command", "")), False)
            return self._json(200, {"allowed": agent.ws.allowed_commands()})
        if self.path == "/api/context":
            # расширение VS Code: какой файл открыт и что выделено; файлы чужих проектов не принимаем
            ok = agent.set_editor(body)
            return self._json(200, {"ok": ok, "project": agent.ws.root.name})
        if self.path == "/api/project":
            root = Path(str(body.get("path", ""))).expanduser().resolve()
            if not root.is_dir():
                return self._json(400, {"error": f"Не вижу такой папки: {root}"})
            try:
                open_project(root)
            except RuntimeError as e:
                return self._json(409, {"error": str(e)})
            return self._json(200, {"ok": True, "project": root.name})

        action = body.get("action")
        skill = agent.ws.skills().get(action[6:]) if isinstance(action, str) and action.startswith("skill:") else None
        if skill:  # своя кнопка-задание: текст читаем заново — файл можно править без перезапуска
            text, label = skill["prompt"], f"[{skill['title']}]"
        else:
            text = TASKS.get(action) or str(body.get("message", "")).strip()
            label = f"[{action}]" if action in TASKS else None
        if not text:
            return self._json(400, {"error": "пустое сообщение"})
        shown = None
        if label is None and (wrapped := with_trace_task(text)) != text:
            shown, text = text, wrapped  # в чате сохранится стектрейс как есть, без служебного задания
        try:
            agent.mood = max(-1.0, min(1.0, float(body.get("mood") or 0)))
        except (TypeError, ValueError):
            agent.mood = 0.0
        if not agent.lock.acquire(blocking=False):
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
            agent.run(text, emit, label, shown)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                emit({"type": "error", "text": str(e)})
            except OSError:
                pass
        finally:
            agent.lock.release()

    def _stt(self, length: int) -> None:
        if self.headers.get("Content-Type") != "audio/wav":
            return self._json(415, {"error": "нужен audio/wav"})
        if not 0 < length <= MAX_AUDIO_BYTES:
            return self._json(413, {"error": "слишком длинная запись"})
        try:
            text = transcribe(self.rfile.read(length), API_KEY)
        except Exception as e:
            return self._json(502, {"error": f"STT: {e}"})
        return self._json(200, {"text": text})

    def _tts(self, text: str, emotion: str = "") -> None:
        if edge_tts is None:
            return self._json(501, {"error": "edge-tts не установлен"})
        if not text:
            return self._json(400, {"error": "пустой текст"})
        try:
            audio = synthesize(text, emotion)
        except Exception as e:
            return self._json(502, {"error": f"TTS: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)
