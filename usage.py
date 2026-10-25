"""
Сколько стоили мозги Айко: расходы OpenRouter по дням в notes/_usage.json (общие на все проекты).
"""

import datetime
import json
import threading

import workspace

KEEP_DAYS = 60
_lock = threading.Lock()


def _load() -> dict:
    try:
        data = json.loads(workspace.USAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def add(usage: dict) -> dict:
    """Учесть usage из ответа OpenRouter; вернуть итог за сегодня."""
    try:
        cost = float(usage.get("cost") or 0)
        tokens = int(usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        return today()
    day = f"{datetime.date.today():%Y-%m-%d}"
    with _lock:
        data = _load()
        d = data.setdefault(day, {"cost": 0.0, "tokens": 0, "requests": 0})
        d["cost"] = round(d["cost"] + cost, 8)
        d["tokens"] += tokens
        d["requests"] += 1
        data = dict(sorted(data.items())[-KEEP_DAYS:])
        workspace.USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        workspace.USAGE_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return d


def today() -> dict:
    return _load().get(f"{datetime.date.today():%Y-%m-%d}", {"cost": 0.0, "tokens": 0, "requests": 0})
