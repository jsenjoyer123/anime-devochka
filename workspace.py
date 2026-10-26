"""
Песочница Айко: инструменты видят только файлы внутри корня проекта. Менять файлы она может
только через plan_edit/apply_edit — и только после подтверждения семпая (это решает agent.py).
Здесь же живёт её память — пути к заметкам и списку проектов.
"""

import datetime
import difflib
import fnmatch
import json
import os
import re
from pathlib import Path

import devtools
from quest import Quest
from tasks import Tasks

HERE = Path(__file__).resolve().parent
NOTES_DIR = HERE / "notes"
SENPAI_PATH = NOTES_DIR / "_senpai.md"      # что Айко помнит о семпае — общее для всех проектов
SKILLS_DIR = HERE / "skills"                 # свои кнопки-задания: skills/*.md
RECENT_PATH = NOTES_DIR / "_projects.json"  # недавние проекты для переключателя
USAGE_PATH = NOTES_DIR / "_usage.json"      # расходы OpenRouter по дням
MAX_SENPAI_LINES = 60
MAX_PROJECT_MEMORY_LINES = 60
MAX_ALLOWED_COMMANDS = 30
STALE_AFTER_COMMITS = 25  # столько коммитов после заметок — пора переизучить проект
MAX_RECENT = 10

MAX_READ_CHARS = 20_000     # символов файла за одно чтение
MAX_FILE_BYTES = 2_000_000  # файлы крупнее не ищем по ним
MAX_LIST_ENTRIES = 300
MAX_SHOW_LINES = 60  # строк кода на экран стрима за один раз
MAX_EDIT_BYTES = 500_000  # крупнее файлы не правим
MAX_EDITS = 20            # замен за один вызов
MAX_SKILLS = 12
MAX_SKILL_CHARS = 6_000
SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

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

ACCESSORIES = ["none", "glasses", "cap", "headphones", "cat_ears", "hard_hat", "beret", "bow",
               "witch_hat", "santa_hat"]
BACKGROUNDS = ["sakura", "terminal", "space", "cafe", "ocean", "sunset", "forest"]

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_outfit(args: dict) -> dict:
    """Проверяем, что модель прислала, — во фронтенд уходят только безопасные значения."""
    def color(key: str, default: str) -> str:
        v = str(args.get(key) or "").strip()
        return v if HEX_RE.match(v) else default

    accessory = args.get("accessory")
    background = args.get("background")
    return {
        "name": str(args.get("name") or "Новый образ").strip()[:32],
        "top_color": color("top_color", "#ffffff"),
        "bottom_color": color("bottom_color", "#ffffff"),
        "shoes_color": color("shoes_color", "#ffffff"),
        "accessory": accessory if accessory in ACCESSORIES else "none",
        "accessory_color": color("accessory_color", "#ff8fc7"),
        "badge_text": str(args.get("badge_text") or "").strip()[:3],
        "badge_color": color("badge_color", "#e2336b"),
        "background": background if background in BACKGROUNDS else "sakura",
    }


class Workspace:
    """Песочница: инструменты видят только файлы внутри корня проекта; правки — только с подтверждением."""

    def __init__(self, root: Path, notes_path: Path):
        self.root = root.resolve()
        self.notes_path = notes_path
        self.outfit_path = notes_path.with_suffix(".outfit.json")
        self.onboarding_path = notes_path.with_suffix(".ONBOARDING.md")
        self.chat_path = notes_path.with_suffix(".chat.json")
        # договорённости и поправки — отдельно от заметок: «Переизучить» перезаписывает заметки целиком
        self.memory_path = notes_path.with_suffix(".memory.md")
        self.allowed_path = notes_path.with_suffix(".allowed.json")  # команды «разрешать всегда»
        self.commits_path = notes_path.with_suffix(".commits.json")  # её коммиты: отменять можно только их
        self.summary_path = notes_path.with_suffix(".summary.md")  # резюме старой части разговора
        self.quest = Quest(notes_path.with_suffix(".quest.json"))
        self.tasks = Tasks(notes_path.with_suffix(".tasks.json"))

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

    @staticmethod
    def _append_fact(path: Path, fact: str, limit: int) -> str:
        fact = " ".join(str(fact).split())[:300]
        if not fact:
            return "Нечего запоминать."
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        lines.append(f"- {datetime.date.today():%d.%m.%Y}: {fact}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines[-limit:]) + "\n", encoding="utf-8")
        return "Запомнила."

    def remember(self, fact: str) -> str:
        return self._append_fact(SENPAI_PATH, fact, MAX_SENPAI_LINES)

    def remember_project(self, fact: str) -> str:
        return self._append_fact(self.memory_path, fact, MAX_PROJECT_MEMORY_LINES)

    def notes_staleness(self) -> int:
        """Сколько коммитов прошло с последнего изучения; 0 — заметок нет или они свежие."""
        try:
            written = self.notes_path.stat().st_mtime
        except OSError:
            return 0
        n = devtools.commits_since(self.root, written)
        return n if n >= STALE_AFTER_COMMITS else 0

    def skills(self) -> dict[str, dict]:
        """Кнопки-задания: skills/*.md самой Айко и .aiko/skills/*.md проекта (проектные важнее).
        Формат: «# Название» первой строкой, «> подсказка» по желанию, дальше — текст задания."""
        found: dict[str, dict] = {}
        for folder in (SKILLS_DIR, self.root / ".aiko" / "skills"):
            try:
                files = sorted(folder.glob("*.md"))
            except OSError:
                continue
            for f in files:
                if not SKILL_ID_RE.match(f.stem) or not f.is_file():
                    continue
                try:
                    lines = f.read_text(encoding="utf-8")[:MAX_SKILL_CHARS].splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                if not lines or not lines[0].startswith("# "):
                    continue
                hint = lines[1][2:].strip() if len(lines) > 1 and lines[1].startswith("> ") else ""
                prompt = "\n".join(lines[2 if hint else 1:]).strip()
                if prompt:
                    found[f.stem] = {"title": lines[0][2:].strip()[:24], "hint": hint[:200], "prompt": prompt}
        return dict(list(found.items())[:MAX_SKILLS])

    def project_memory(self) -> str:
        return self.memory_path.read_text(encoding="utf-8") if self.memory_path.is_file() else ""

    def allowed_commands(self) -> list[str]:
        try:
            items = json.loads(self.allowed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [c for c in items if isinstance(c, str)] if isinstance(items, list) else []

    def set_allowed(self, command: str, allowed: bool) -> None:
        """Команды сравниваются целиком, до символа: «make test» не разрешает «make test; rm -rf ~»."""
        items = [c for c in self.allowed_commands() if c != command]
        if allowed:
            items.append(command)
        self.allowed_path.parent.mkdir(parents=True, exist_ok=True)
        self.allowed_path.write_text(json.dumps(items[-MAX_ALLOWED_COMMANDS:], ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    def own_commits(self) -> list[str]:
        try:
            items = json.loads(self.commits_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [c for c in items if isinstance(c, str)] if isinstance(items, list) else []

    def set_own_commits(self, items: list[str]) -> None:
        self.commits_path.parent.mkdir(parents=True, exist_ok=True)
        self.commits_path.write_text(json.dumps(items[-20:], indent=1), encoding="utf-8")

    def write_onboarding_doc(self, content: str) -> str:
        self.onboarding_path.parent.mkdir(parents=True, exist_ok=True)
        self.onboarding_path.write_text(content, encoding="utf-8")
        return "ONBOARDING.md сохранён, семпай может скачать его кнопкой в чате"

    def plan_edit(self, path: str, edits: list | None = None, content: str | None = None) -> dict:
        """Готовим правку, ничего не записывая: проверки + diff для семпая.
        edits — замены [{old, new}], каждая old должна встречаться в файле ровно один раз;
        content — текст нового файла (только если файла ещё нет)."""
        p = self._resolve(path)
        rel = self._rel(p)
        if p == self.root or self._is_secret(p.name) or any(part in IGNORED_DIRS for part in Path(rel).parts[:-1]):
            raise ValueError(f"Этот файл я не трогаю: {path}")
        if content is not None:
            if p.exists():
                raise ValueError(f"{rel} уже есть — правь его заменами (edits), а не content")
            before, after = "", str(content)
        else:
            if not p.is_file():
                raise ValueError(f"Файл не найден: {rel}. Новый файл — через content")
            if p.stat().st_size > MAX_EDIT_BYTES or self._is_binary(p):
                raise ValueError(f"{rel} слишком большой или бинарный — не правлю")
            raw = self._read_raw(p)
            before = raw.replace("\r\n", "\n")
            if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
                raise ValueError("Нужны old (точный фрагмент файла) и new (на что заменить) "
                                 f"или edits — список таких замен (до {MAX_EDITS})")
            after = before
            for i, e in enumerate(edits, 1):
                old, new = str((e or {}).get("old", "")), str((e or {}).get("new", ""))
                n = after.count(old) if old else 0
                if n != 1:
                    raise ValueError(f"Замена {i}: фрагмент old найден {n} раз, нужен ровно 1 — "
                                     "скопируй его из read_file точно, с отступами, и добавь строк для уникальности")
                after = after.replace(old, new, 1)
            if after == before:
                raise ValueError("Правка ничего не меняет")
        if len(after.encode()) > MAX_EDIT_BYTES:
            raise ValueError("Слишком большой результат")
        diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                            "/dev/null" if content is not None else f"a/{rel}", f"b/{rel}"))
        crlf = content is None and "\r\n" in raw
        return {"path": rel, "before": before, "after": after, "diff": diff, "new": content is not None, "crlf": crlf}

    @staticmethod
    def _read_raw(p: Path) -> str:
        with p.open(encoding="utf-8", newline="") as f:  # без перевода \r\n → \n
            return f.read()

    def revert_plan(self, plan: dict) -> dict:
        """Обратная правка: вернуть файл к виду до plan, если после неё его никто не менял."""
        p = self._resolve(plan["path"])
        now = self._read_raw(p).replace("\r\n", "\n") if p.is_file() else None
        if now != plan["after"]:
            raise ValueError(f"{plan['path']} уже поменяли после моей правки — откатывать вслепую не буду")
        diff = "".join(difflib.unified_diff(plan["after"].splitlines(keepends=True), plan["before"].splitlines(keepends=True),
                                            f"a/{plan['path']}", "/dev/null" if plan["new"] else f"b/{plan['path']}"))
        return {**plan, "before": plan["after"], "after": plan["before"], "diff": diff, "revert_new": plan["new"],
                "new": False}

    def apply_edit(self, plan: dict) -> str:
        """Записываем подтверждённую правку, если файл не изменился с момента plan_edit."""
        p = self._resolve(plan["path"])
        now = self._read_raw(p).replace("\r\n", "\n") if p.is_file() else ""
        if now != plan["before"] or (plan["new"] and p.exists()):
            raise ValueError(f"{plan['path']} изменился, пока семпай смотрел, — перечитай и предложи заново")
        if plan.get("revert_new"):  # откат создания файла — файл удаляем
            p.unlink()
            return f"Удалила созданный мной {plan['path']}"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline="") as f:  # переводы строк — как были в файле
            f.write(plan["after"].replace("\n", "\r\n") if plan["crlf"] else plan["after"])
        added = sum(1 for line in plan["diff"].splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in plan["diff"].splitlines() if line.startswith("-") and not line.startswith("---"))
        return f"{'Создала' if plan['new'] else 'Поправила'} {plan['path']}: +{added} −{removed}"

    def code_snippet(self, path: str, start_line: int, end_line: int) -> dict:
        """Фрагмент для экрана стрима: без номеров строк, не больше MAX_SHOW_LINES."""
        p = self._resolve(path)
        if self._is_secret(p.name) or not p.is_file() or self._is_binary(p):
            raise ValueError(f"Не могу показать {path}")
        start = max(1, int(start_line))
        end = min(int(end_line), start + MAX_SHOW_LINES - 1)
        lines = []
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if n > end:
                    break
                if n >= start:
                    lines.append(line.rstrip("\n")[:300])
        return {"path": self._rel(p), "start": start, "code": "\n".join(lines)}

    def diff_snippet(self, path: str | None = None, staged: bool = False, commit: str | None = None) -> str:
        rel = None
        if path:
            p = self._resolve(path)
            if self._is_secret(p.name):
                raise ValueError(f"Не могу показать {path}")
            rel = self._rel(p)
        return devtools.screen_diff(self.root, rel, bool(staged), commit or None)

    # git — только чтение; пути проверяем так же, как для файлов
    def git_log(self, path: str | None = None, max_count: int = 20) -> str:
        rel = self._rel(self._resolve(path)) if path else None
        return devtools.git_log(self.root, rel, max_count)

    def git_show(self, commit: str) -> str:
        return devtools.git_show(self.root, commit)

    def git_blame(self, path: str, start_line: int, end_line: int) -> str:
        return devtools.git_blame(self.root, self._rel(self._resolve(path)), start_line, end_line)

    def git_hotspots(self) -> str:
        return devtools.git_hotspots(self.root)

    def git_diff(self, staged: bool = False) -> str:
        return devtools.git_diff(self.root, bool(staged))

    def github_issues(self, label: str | None = None, limit: int = 15) -> str:
        return devtools.github_issues(self.root, label, limit)

    def github_issue(self, number: int) -> str:
        return devtools.github_issue(self.root, number)

    def branch_diff(self, base: str | None = None) -> str:
        return devtools.branch_diff(self.root, base)

    def ci_status(self) -> str:
        return devtools.ci_status(self.root)

    def add_task(self, text: str) -> str:
        t = self.tasks.add(text)
        return f"Записала в план: №{t['id']} {t['text']}"

    def complete_task(self, id: int) -> str:
        t = self.tasks.set_done(id)
        return f"Задача №{t['id']} сделана: {t['text']}"

    def remove_task(self, id: int) -> str:
        self.tasks.remove(id)
        return f"Убрала задачу №{id} из плана"

    def complete_quest(self, step: str) -> str:
        first = self.quest.complete(step)
        st = self.quest.state()
        return (f"Шаг отмечен! Теперь {st['xp']} XP, уровень «{st['level']}»." if first
                else "Этот шаг уже был выполнен раньше.")

    def call(self, name: str, args: dict) -> str:
        fn = {"list_dir": self.list_dir, "read_file": self.read_file, "search": self.search,
              "write_notes": self.write_notes, "set_outfit": self.set_outfit,
              "write_onboarding_doc": self.write_onboarding_doc, "remember": self.remember,
              "remember_project": self.remember_project,
              "git_log": self.git_log, "git_show": self.git_show, "git_blame": self.git_blame,
              "git_hotspots": self.git_hotspots, "git_diff": self.git_diff,
              "github_issues": self.github_issues, "github_issue": self.github_issue,
              "complete_quest": self.complete_quest, "add_task": self.add_task, "ci_status": self.ci_status,
              "branch_diff": self.branch_diff,
              "complete_task": self.complete_task, "remove_task": self.remove_task}.get(name)
        if fn is None:
            return f"Неизвестный инструмент: {name}"
        try:
            return fn(**args)
        except Exception as e:  # ошибку отдаём модели, пусть сама разберётся
            return f"Ошибка: {type(e).__name__}: {e}"
