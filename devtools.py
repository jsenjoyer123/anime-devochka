"""
Инструменты разработчика для Айко: git и GitHub только на чтение, запуск команд (после подтверждения).
Всё с таймаутами и ограничением вывода — чтобы огромный лог не съел память.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

GIT_TIMEOUT = 15
MAX_GIT_CHARS = 15_000
RUN_TIMEOUT = 180
MAX_RUN_CHARS = 8_000

REV_RE = re.compile(r"^(HEAD(~\d+|\^+)?|[0-9a-fA-F]{4,40})$")

# Никаких пейджеров, внешних diff-программ и textconv-фильтров из конфига репозитория
GIT_BASE = ["git", "-c", "core.pager=cat", "-c", "color.ui=never", "--no-optional-locks"]


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            [*GIT_BASE, "-C", str(root), *args],
            capture_output=True, text=True, errors="replace", timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError:
        return "git не установлен."
    except subprocess.TimeoutExpired:
        return "git думал слишком долго, прервала."
    out = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
    if "not a git repository" in out:
        return "Это не git-репозиторий."
    if len(out) > MAX_GIT_CHARS:
        out = out[:MAX_GIT_CHARS] + "\n... (обрезано)"
    return out.strip() or "(пусто)"


def git_log(root: Path, rel: str | None = None, max_count: int = 20) -> str:
    n = max(1, min(int(max_count), 100))
    args = ["log", f"-n{n}", "--date=short", "--format=%h %ad %an: %s"]
    return _git(root, *args, *(["--", rel] if rel else []))


def git_show(root: Path, rev: str) -> str:
    if not REV_RE.match(rev):
        return "Некорректный коммит: нужен хеш или HEAD~N."
    return _git(root, "show", "--stat", "--patch", "--no-ext-diff", "--no-textconv", rev)


def git_blame(root: Path, rel: str, start: int, end: int) -> str:
    start, end = max(1, int(start)), max(1, int(end))
    if end < start or end - start > 200:
        return "Диапазон строк: не больше 200."
    return _git(root, "blame", "--date=short", f"-L{start},{end}", "--", rel)


def git_hotspots(root: Path, commits: int = 500) -> str:
    """Самые часто меняемые файлы — там обычно сердце проекта (или боль)."""
    out = _git(root, "log", f"-n{max(10, min(int(commits), 2000))}", "--format=", "--name-only")
    if out.startswith(("Это не", "git ")):
        return out
    counts = Counter(line for line in out.splitlines() if line.strip())
    return "\n".join(f"{n:>4}  {path}" for path, n in counts.most_common(20)) or "(истории нет)"


def git_diff(root: Path, staged: bool = False) -> str:
    args = ["diff", "--stat", "--patch", "--no-ext-diff", "--no-textconv"]
    if staged:
        args.append("--staged")
    # новые файлы в diff не попадают — показываем их списком из status
    status = _git(root, "status", "--short", "--untracked-files=all")
    return f"git status:\n{status}\n\n{_git(root, *args)}"


def changes(root: Path) -> dict:
    """Что поменялось в рабочей копии: подпись (чтобы заметить новые правки) и список файлов."""
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    if status.startswith(("Это не", "git ")):
        return {"git": False, "sig": "", "files": []}
    numstat = _git(root, "diff", "--numstat", "--no-ext-diff", "--no-textconv")
    # _git срезает пробелы по краям, поэтому у первой строки может не быть ведущего пробела статуса
    files = [line.split(maxsplit=1)[1] for line in status.splitlines() if len(line.split(maxsplit=1)) == 2]
    sig = hashlib.sha1((status + numstat).encode()).hexdigest() if files else ""
    return {"git": True, "sig": sig, "files": files[:50]}


GITHUB_API = "https://api.github.com"
GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def github_repo(root: Path) -> tuple[str, str] | None:
    """owner/repo из адреса origin, если проект живёт на GitHub."""
    url = _git(root, "remote", "get-url", "origin")
    m = GITHUB_REMOTE_RE.search(url.strip())
    return (m.group(1), m.group(2)) if m else None


def _github(path: str) -> list | dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "aiko-chan"}
    if os.environ.get("GITHUB_TOKEN"):  # для приватных репозиториев и большего лимита запросов
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    req = urllib.request.Request(GITHUB_API + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("Репозиторий не найден (приватный? добавь GITHUB_TOKEN в .env)") from e
        if e.code == 403:
            raise RuntimeError("GitHub ограничил запросы — попробуй позже или добавь GITHUB_TOKEN") from e
        raise RuntimeError(f"GitHub HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"GitHub недоступен (нет интернета?): {e.reason}") from e


def github_issues(root: Path, label: str | None = None, limit: int = 15) -> str:
    repo = github_repo(root)
    if not repo:
        return "У проекта нет origin на GitHub."
    limit = max(1, min(int(limit), 30))
    # PR приходят в том же списке и могут занять всю страницу — берём с запасом и фильтруем
    query = {"state": "open", "per_page": "100", "sort": "updated"}
    if label:
        query["labels"] = label
    items = _github(f"/repos/{repo[0]}/{repo[1]}/issues?" + urllib.parse.urlencode(query))
    lines = []
    for it in items:
        if "pull_request" in it:
            continue  # GitHub отдаёт PR в том же списке
        labels = ", ".join(lb["name"] for lb in it.get("labels", []))
        body = " ".join((it.get("body") or "").split())[:200]
        lines.append(f"#{it['number']} {it['title']} [{labels}] 💬{it.get('comments', 0)}\n    {body}")
        if len(lines) >= limit:
            break
    return f"{repo[0]}/{repo[1]}, открытые задачи:\n" + "\n".join(lines) if lines else "Открытых задач не нашлось."


def github_issue(root: Path, number: int) -> str:
    repo = github_repo(root)
    if not repo:
        return "У проекта нет origin на GitHub."
    base = f"/repos/{repo[0]}/{repo[1]}/issues/{int(number)}"
    it = _github(base)
    out = [f"#{it['number']} {it['title']} ({it['state']})", (it.get("body") or "")[:4000]]
    if it.get("comments"):
        for c in _github(base + "/comments?per_page=5"):
            out.append(f"— {c['user']['login']}: {(c.get('body') or '')[:800]}")
    return "\n\n".join(out)


def run_command(root: Path, command: str) -> str:
    """Запуск команды в корне проекта. Вызывать ТОЛЬКО после подтверждения семпая."""
    # вывод пишем во временный файл, а не в память, и читаем только хвост
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(
            command, shell=True, cwd=root, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            code = proc.wait(timeout=RUN_TIMEOUT)
            status = f"код выхода {code}"
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            status = f"прервано по таймауту {RUN_TIMEOUT} с"
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - MAX_RUN_CHARS))
        tail = out.read().decode("utf-8", errors="replace")
    prefix = "... (начало вывода обрезано)\n" if size > MAX_RUN_CHARS else ""
    return f"$ {command}\n[{status}]\n{prefix}{tail}"
