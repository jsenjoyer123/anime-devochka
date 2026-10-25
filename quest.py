"""
Онбординг-квест: шаги, которые проходит новичок в проекте, опыт и уровни.
Прогресс хранится рядом с заметками: notes/<проект>.quest.json.
"""

import datetime
import json
from pathlib import Path

# id, название, опыт; порядок — рекомендуемый путь новичка
STEPS = [
    ("learn", "Айко изучила проект", 30),
    ("tour", "Пройти экскурсию по коду", 40),
    ("architecture", "Разобраться в архитектуре по схеме", 30),
    ("history", "Узнать историю проекта из git", 20),
    ("run", "Запустить проект локально", 50),
    ("tests", "Прогнать тесты", 50),
    ("first_task", "Выбрать первую задачу", 40),
    ("review", "Получить ревью своих изменений", 40),
    ("quiz", "Сдать экзамен Айко", 60),
    ("doc", "Оставить команде ONBOARDING.md", 30),
]
STEP_IDS = [s[0] for s in STEPS]

LEVELS = [(0, "Новичок"), (100, "Стажёр"), (200, "Джун проекта"), (300, "Свой человек"), (390, "Легенда проекта")]


class Quest:
    def __init__(self, path: Path):
        self.path = path

    def done(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("done", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def complete(self, step: str) -> bool:
        """Отметить шаг. Возвращает True, если он выполнен впервые."""
        if step not in STEP_IDS:
            raise ValueError(f"Нет такого шага: {step}. Есть: {', '.join(STEP_IDS)}")
        done = self.done()
        if step in done:
            return False
        done[step] = f"{datetime.date.today():%d.%m.%Y}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"done": done}, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def state(self) -> dict:
        done = self.done()
        xp = sum(x for sid, _, x in STEPS if sid in done)
        level = [name for need, name in LEVELS if xp >= need][-1]
        nxt = next((need for need, _ in LEVELS if need > xp), None)
        return {
            "xp": xp,
            "level": level,
            "next_level_xp": nxt,
            "max_xp": sum(x for *_, x in STEPS),
            "steps": [{"id": sid, "title": t, "xp": x, "done": sid in done} for sid, t, x in STEPS],
        }

    def prompt_hint(self) -> str:
        """Кусочек системного промпта: где семпай в квесте и что предложить дальше."""
        st = self.state()
        todo = [s["title"] for s in st["steps"] if not s["done"]]
        if not todo:
            return f"Онбординг-квест пройден целиком ({st['xp']} XP, «{st['level']}») — можно этим гордиться.\n"
        return (f"Онбординг-квест семпая: {st['xp']} XP, уровень «{st['level']}». "
                f"Впереди: {'; '.join(todo[:3])}. Иногда к месту предлагай следующий шаг.\n")
