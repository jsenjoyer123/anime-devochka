"""
Инструменты разработчика для Айко: git только на чтение и запуск команд (после подтверждения).
Всё с таймаутами и ограничением вывода — чтобы огромный лог не съел память.
"""

import os
import re
import signal
import subprocess
import tempfile
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
