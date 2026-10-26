"""
План задач проекта: чеклист, который Айко ведёт вместе с семпаем (notes/<проект>.tasks.json).
"""

import datetime
import json
import threading
from pathlib import Path

MAX_TASKS = 40
MAX_TEXT = 200


class Tasks:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()  # меняют и Айко (ход агента), и семпай (галочки в панели)

    def items(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = [t for t in data if isinstance(t, dict) and "id" in t] if isinstance(data, list) else []
        return sorted(items, key=lambda t: bool(t.get("done")))  # открытые — сверху

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, text: str) -> dict:
        text = " ".join(str(text).split())[:MAX_TEXT]
        if not text:
            raise ValueError("Пустая задача")
        with self.lock:
            items = self.items()
            if sum(not t["done"] for t in items) >= MAX_TASKS:
                raise ValueError(f"В плане уже {MAX_TASKS} открытых задач — сначала закрой или убери старые")
            task = {"id": max((t["id"] for t in items), default=0) + 1, "text": text, "done": False,
                    "added": f"{datetime.date.today():%d.%m.%Y}"}
            # закрытые задачи храним недолго: только последние 20
            self._save([t for t in items if not t["done"]] + [task] + [t for t in items if t["done"]][-20:])
            return task

    def set_done(self, task_id: int, done: bool = True) -> dict:
        with self.lock:
            items = self.items()
            task = next((t for t in items if t["id"] == int(task_id)), None)
            if task is None:
                raise ValueError(f"Нет задачи №{task_id}")
            task["done"] = bool(done)
            self._save(items)
            return task

    def remove(self, task_id: int) -> None:
        with self.lock:
            items = self.items()
            if not any(t["id"] == int(task_id) for t in items):
                raise ValueError(f"Нет задачи №{task_id}")
            self._save([t for t in items if t["id"] != int(task_id)])

    def prompt_hint(self) -> str:
        open_ = [t for t in self.items() if not t["done"]]
        if not open_:
            return ""
        return ("\nПлан задач (ведёшь вместе с семпаем, отмечай сделанное через complete_task):\n"
                + "\n".join(f"- №{t['id']} {t['text']}" for t in open_) + "\n")
