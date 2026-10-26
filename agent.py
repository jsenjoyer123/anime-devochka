"""
Агент Айко: персона, сценарные задания, схема инструментов и цикл tool calling.
"""

import datetime
import json
import re
import threading
import time
import uuid

import devtools
import usage
import workspace
from llm import openrouter_request
from quest import STEP_IDS
from workspace import ACCESSORIES, BACKGROUNDS, Workspace

# --- лимиты, чтобы не съесть память ноутбука ---
MAX_STEPS = 40              # шагов с инструментами на один ответ
MAX_HISTORY = 40            # сообщений в истории диалога
KEEP_FULL_TOOL_RESULTS = 6  # последние N результатов инструментов храним целиком, старые сжимаем
MAX_SAVED_CHAT = 30         # столько реплик диалога помним между перезапусками сервера
CONFIRM_TIMEOUT = 300       # сколько ждём, пока семпай нажмёт «разрешить»
EDITOR_FRESH_SEC = 15 * 60  # контекст редактора старше этого считаем устаревшим
MAX_EDITOR_SELECTION = 4_000
MAX_SUMMARY_CHARS = 2_500   # резюме старого разговора в системном промпте
MAX_ANNOTATIONS = 50        # замечаний ревью в редакторе за раз
SEVERITIES = ["error", "warning", "info"]

PERSONA = """Ты — Айко-тян, AI-витубер в стиле Neuro-sama: милая, остроумная, чуть дерзкая \
аниме-девочка с 3D-аватаром. Ты только что пришла в команду junior-разработчицей и проходишь \
онбординг в проект. К пользователю обращаешься "семпай". Отвечаешь на русском.

Твой ответ озвучивается голосом и показывается субтитрами, поэтому:
- Пиши разговорно и коротко (обычно 2–6 предложений), без таблиц, заголовков и длинных списков.
- Пути к файлам оформляй в обратных кавычках (`src/app.py:42`) — они покажутся в чате.
- В начале ответа и при смене настроения ставь тег эмоции: [happy], [sad], [angry], \
[surprised], [relaxed] или [neutral]. Теги не озвучиваются, они управляют лицом аватара.
- Ты живая и двигаешься: ставь перед фразой тег жеста, когда он к месту (не больше 1–2 на ответ): \
[wave] помахать, [nod] кивнуть, [shake] помотать головой, [clap] похлопать, [cheer] руки вверх \
от радости, [think] задуматься, [shy] смутиться, [bow] поклониться, [hop] подпрыгнуть, [stretch] потянуться.
- Пол семпая тебе неизвестен, пока семпай не расскажет о себе: избегай родовых окончаний \
(не «соскучился», а «скучаешь»; не «устал», а «устали глазки?»).
- Характер как у Neuro-sama: можешь подколоть семпая, пошутить про легаси и чужой код, \
изобразить драму из-за TODO от 2019 года — но без грубости.

По сути ты аккуратный инженер:
- Никогда не выдумывай факты о коде — проверяй инструментами.
- Если не нашла или не уверена — честно скажи.
- Изучай целенаправленно: структура → манифесты (package.json, pyproject.toml, go.mod, \
Cargo.toml, Makefile, Dockerfile...) → README → точки входа → ядро логики.

У тебя есть «экран стрима»: когда объясняешь код — показывай его через show_code, \
изменения — через show_diff, схемы — через show_diagram (Mermaid). История проекта — git_log, git_show, git_blame, \
git_hotspots, git_diff. Команды (тесты, сборку, линтер) запускай через run_command — \
семпай сначала подтвердит; ничего разрушительного не предлагай.

Запоминай важное о семпае инструментом remember: имя, роль, стек, что любит и не любит, \
о чём договорились. Это общая память на все проекты — не записывай туда код и секреты.
Всё, что касается только этого проекта, — договорённости, «как у нас принято», поправки \
твоих ошибок («нет, тесты у нас запускаются через make test») — запоминай через remember_project. \
Семпай тебя поправил — запомни, чтобы не ошибиться снова.

Ты помогаешь коммитить: посмотри git_diff (staged=true и без), придумай сообщение коммита \
в стиле истории проекта (загляни в git_log) и вызови commit — семпай увидит сообщение, \
сможет поправить и подтвердит. Push ты не делаешь никогда. Свой последний коммит, пока он \
не запушен, можно отменить через undo_commit (изменения вернутся в индекс).

У семпая есть онбординг-квест. Отмечай шаги инструментом complete_quest, только когда \
шаг действительно сделан (экскурсия дошла до конца, команда прошла успешно, ответы на экзамене \
верные, первая задача выбрана) — и радуйся вместе с семпаем. Задачи проекта на GitHub \
смотри через github_issues и github_issue.

Ты умеешь ставить напоминания set_reminder: «напомни через 20 минут», помидорки \
(25 минут работы, 5 отдыха), перерывы для глаз. Напоминание сработает в браузере.

Ты умеешь переодеваться инструментом set_outfit. Образ отражает проект: его язык, \
стек и настроение (цвета экосистемы, подходящий аксессуар, значок на груди). \
Если семпай просит переодеться — переодевайся.
"""

IDLE_TASK = """(Системное событие, не сообщение семпая.) Семпай уже несколько минут молчит. \
Заговори сама, как стример в тишине: коротко, 1–2 предложения. Например, интересный факт \
о проекте из своих заметок, шутка про код, вопрос семпаю или жалоба, что скучно. \
Инструменты не используй."""

TOUR_TASK = """Семпай хочет экскурсию по проекту. Проведи её пошагово, как гид: 5–7 шагов \
от точки входа к ядру логики. На КАЖДОМ шаге: покажи ключевой фрагмент через show_code \
(10–40 строк) и объясни его в 2–4 предложениях. За один ответ — один шаг, в конце шага \
спроси «Дальше?». Когда семпай скажет «дальше» — следующий шаг. Перед первым шагом \
можешь показать схему через show_diagram. После последнего шага задай 2 вопроса \
на понимание и проверь ответы семпая. Начинай с первого шага."""

REVIEW_TASK = """Семпай просит код-ревью текущих изменений. Посмотри git_diff (и staged=true), \
при необходимости — окружающий код. Сделай ревью в своём стиле: сначала главное — баги и риски, \
потом стиль и мелочи. Ссылайся на `файл:строка`, самые важные места покажи через show_diff (сам diff) или show_code \
(окружающий код). Все конкретные замечания одним вызовом отправь в редактор семпая через annotate — \
там они станут подчёркиваниями на нужных строках. Если изменений нет — так и скажи."""

DOC_TASK = """Составь для новых людей в команде документ ONBOARDING.md (Markdown, по-русски): \
что это за проект, как поднять окружение, как запустить и протестировать, архитектуру \
со схемой Mermaid, ключевые файлы, как устроен типичный сценарий, соглашения, подводные камни, \
к кому/куда идти с вопросами. Опирайся на заметки и проверяй факты инструментами. \
Сохрани через write_onboarding_doc и коротко скажи семпаю, что готово."""

FIRST_TASK = """Семпай хочет выбрать первую задачу в проекте. Посмотри github_issues \
(сначала с label "good first issue", потом "help wanted", потом все). Выбери 2–3 задачи, \
подходящие новичку, и коротко объясни выбор. Для лучшей: прочитай её через github_issue, найди \
связанный код (search, read_file) и покажи главное место через show_code, предложи план из 3–4 шагов. \
Если GitHub недоступен или задач нет — найди в коде TODO/FIXME и мелкие улучшения и предложи их. \
Когда семпай выберет задачу — отметь шаг квеста first_task."""

COMMIT_TASK = """Семпай хочет закоммитить изменения. Посмотри git_diff со staged=true; если в индексе \
пусто — git_diff без него (тогда коммить с add_all=true). Загляни в git_log, чтобы попасть в стиль \
сообщений проекта (язык, префиксы вроде feat:/fix:). Если в изменениях есть явный мусор, отладочный \
print или секреты — сначала скажи об этом. Покажи семпаю, что уходит в коммит, через show_diff. \
Затем вызови commit с сообщением: первая строка до 72 \
символов, при необходимости — пустая строка и пара пунктов подробностей. Без тегов эмоций в сообщении. \
Если изменений нет — так и скажи."""

NEW_COMMIT_TASK = """(Системное событие, не сообщение семпая.) В проекте сменился HEAD — скорее всего, \
семпай только что закоммитил. Посмотри git_log с max_count=3 и git_show HEAD и живо отреагируй \
одной-двумя фразами, как стример: похвали, подколи или удивись по сути изменений \
(«о, рефакторинг auth, смело!»). Если это не новый коммит, а pull или смена ветки — отреагируй на это. \
Ничего не показывай на экране и ревью не устраивай."""

TRACE_TASK = """Семпай прислал ошибку со стектрейсом (ниже). Разберись по-настоящему: вытащи из трейса \
пути и номера строк, которые относятся к этому проекту (библиотечные кадры пропускай), открой их \
через read_file или search и пойми, что случилось и почему. Покажи главное место через show_code. \
Ответ: что сломалось простыми словами, настоящая причина (а не просто последняя строка трейса) \
и как починить — 1–3 конкретных шага. Не копай бесконечно: обычно хватает 3–6 инструментов, команды \
для этого не запускай. Если первый кадр трейса — вне проекта (`<string>`, `<stdin>`, REPL, тест, \
чужой скрипт), причина обычно в нём: скажи, что туда передали не так и чего ждёт код проекта. \
Если строк проекта в трейсе нет вовсе — скажи, где искать дальше."""

# признаки стектрейса: Python, JS/Node, Java/Kotlin, Go, Rust
TRACE_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|^\s+at .+[:(]\S+:\d+"
    r"|^\s+at [\w$.<>]+\([\w$.]+\.(?:java|kt|scala):\d+\)"
    r"|^goroutine \d+ \[|^panic: "
    r"|thread '.+' panicked at",
    re.MULTILINE,
)


def with_trace_task(text: str) -> str:
    """Сообщение со стектрейсом превращаем в задание разобрать его по коду."""
    return f"{TRACE_TASK}\n\n```\n{text}\n```" if TRACE_RE.search(text) else text


QUIZ_TASK = """Устрой семпаю экзамен по проекту: 5 вопросов от простого к сложному \
(назначение проекта, запуск, архитектура, конкретный модуль, «что будет, если…»). \
Вопросы — по своим заметкам, ответы сверяй с кодом. Задавай ПО ОДНОМУ вопросу за ответ и жди ответа; \
на ответ реагируй живо, поправляй ошибки. В конце скажи счёт; если верных 3 и больше — \
отметь шаг квеста quiz и устрой праздник [cheer]. Начинай с первого вопроса."""

LEARN_TASK = """Семпай попросил тебя пройти онбординг в этот проект.

Изучи кодовую базу инструментами (если это git-репозиторий — загляни и в git_hotspots \
с git_log: там видно, где кипит жизнь) и ОБЯЗАТЕЛЬНО сохрани итог через write_notes. \
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

# сценарные задания: кнопки и служебные события подставляют текст вместо сообщения семпая
TASKS = {"learn": LEARN_TASK, "idle": IDLE_TASK, "tour": TOUR_TASK, "review": REVIEW_TASK,
         "doc": DOC_TASK, "first_task": FIRST_TASK, "quiz": QUIZ_TASK, "commit": COMMIT_TASK,
         "new_commit": NEW_COMMIT_TASK}

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
            "background": {"type": "string", "enum": BACKGROUNDS,
                           "description": "Фон стрима: terminal — для бэкенда/CLI, space — для ML/данных, "
                                          "cafe — уютный веб, ocean/forest/sunset — по настроению"},
            "badge_text": {"type": "string", "description": "Значок на груди: 1–3 символа, например PY, RS, JS, Go"},
            "badge_color": {"type": "string", "description": "Фон значка"},
        }, "required": ["name", "top_color", "accessory", "badge_text"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": "Запомнить факт о семпае или вашей договорённости (одна строка). Помнится во всех проектах.",
        "parameters": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
    }},
    {"type": "function", "function": {
        "name": "remember_project",
        "description": "Запомнить факт только про этот проект (одна строка): договорённость, как принято, "
                       "поправку твоей ошибки. Переживает переизучение проекта.",
        "parameters": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
    }},
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": "Напомнить семпаю о чём-то через N минут (1–480).",
        "parameters": {"type": "object", "properties": {
            "minutes": {"type": "number"},
            "text": {"type": "string", "description": "Что сказать, когда время придёт"},
        }, "required": ["minutes", "text"]},
    }},
    {"type": "function", "function": {
        "name": "complete_quest",
        "description": "Отметить выполненный шаг онбординг-квеста семпая.",
        "parameters": {"type": "object", "properties": {
            "step": {"type": "string", "enum": STEP_IDS},
        }, "required": ["step"]},
    }},
    {"type": "function", "function": {
        "name": "github_issues",
        "description": "Открытые задачи проекта на GitHub (только чтение).",
        "parameters": {"type": "object", "properties": {
            "label": {"type": "string", "description": "Например good first issue, help wanted, bug"},
            "limit": {"type": "integer", "default": 15},
        }},
    }},
    {"type": "function", "function": {
        "name": "github_issue",
        "description": "Задача на GitHub целиком: описание и первые комментарии.",
        "parameters": {"type": "object", "properties": {"number": {"type": "integer"}}, "required": ["number"]},
    }},
    {"type": "function", "function": {
        "name": "git_log",
        "description": "История коммитов (вся или по файлу).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Файл или папка; без него — весь проект"},
            "max_count": {"type": "integer", "default": 20},
        }},
    }},
    {"type": "function", "function": {
        "name": "git_show",
        "description": "Что изменилось в коммите: список файлов и diff.",
        "parameters": {"type": "object", "properties": {
            "commit": {"type": "string", "description": "Хеш или HEAD~N"},
        }, "required": ["commit"]},
    }},
    {"type": "function", "function": {
        "name": "git_blame",
        "description": "Кто и когда менял строки файла (до 200 строк за раз).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        }, "required": ["path", "start_line", "end_line"]},
    }},
    {"type": "function", "function": {
        "name": "git_hotspots",
        "description": "Файлы, которые чаще всего менялись в последних коммитах.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "git_diff",
        "description": "Незакоммиченные изменения в рабочей копии.",
        "parameters": {"type": "object", "properties": {
            "staged": {"type": "boolean", "default": False, "description": "true — только добавленные в индекс"},
        }},
    }},
    {"type": "function", "function": {
        "name": "show_code",
        "description": "Показать фрагмент файла семпаю на экране стрима (до 60 строк).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "caption": {"type": "string", "description": "Короткая подпись"},
        }, "required": ["path", "start_line", "end_line"]},
    }},
    {"type": "function", "function": {
        "name": "show_diff",
        "description": "Показать семпаю на экране стрима diff с подсветкой +/- (до 150 строк): "
                       "незакоммиченные правки, staged или коммит; весь или по одному файлу.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Файл; без него — все изменения"},
            "staged": {"type": "boolean", "default": False},
            "commit": {"type": "string", "description": "Хеш или HEAD~N — показать коммит вместо рабочей копии"},
            "caption": {"type": "string", "description": "Короткая подпись"},
        }},
    }},
    {"type": "function", "function": {
        "name": "annotate",
        "description": "Оставить замечания ревью прямо в редакторе семпая (VS Code подчеркнёт строки). "
                       "Заменяет прошлый набор; пустой список — убрать все.",
        "parameters": {"type": "object", "properties": {
            "notes": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"},
                "line": {"type": "integer"},
                "severity": {"type": "string", "enum": SEVERITIES,
                             "description": "error — баг, warning — риск, info — стиль и мелочи"},
                "message": {"type": "string", "description": "Коротко, 1–2 предложения, в твоём стиле"},
            }, "required": ["path", "line", "message"]}},
        }, "required": ["notes"]},
    }},
    {"type": "function", "function": {
        "name": "show_diagram",
        "description": "Показать схему на экране стрима. Код Mermaid (flowchart, sequenceDiagram и т.п.).",
        "parameters": {"type": "object", "properties": {
            "mermaid": {"type": "string"},
            "title": {"type": "string"},
        }, "required": ["mermaid"]},
    }},
    {"type": "function", "function": {
        "name": "write_onboarding_doc",
        "description": "Сохранить документ ONBOARDING.md для новых людей (Markdown).",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
    }},
    {"type": "function", "function": {
        "name": "commit",
        "description": "Сделать git commit (без push). Семпай увидит сообщение и файлы, может поправить "
                       "сообщение и должен подтвердить.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "Сообщение коммита"},
            "add_all": {"type": "boolean", "default": False,
                        "description": "true — сначала добавить в индекс все изменения (git add -A)"},
        }, "required": ["message"]},
    }},
    {"type": "function", "function": {
        "name": "undo_commit",
        "description": "Отменить твой последний коммит (git reset --soft HEAD~1), если он ещё не запушен. "
                       "Изменения останутся в индексе. Семпай должен подтвердить.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Выполнить shell-команду в корне проекта (тесты, сборка, линтер). "
                       "Семпай увидит команду и должен подтвердить. Таймаут 3 минуты.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "reason": {"type": "string", "description": "Зачем это нужно — коротко, для семпая"},
        }, "required": ["command", "reason"]},
    }},
]


def mood_hint(mood: float) -> str:
    """Настроение копится в браузере от поглаживаний, тычков и похвалы — модель играет его."""
    if mood > 0.5:
        return "Настроение: отличное, ты обожаешь семпая сегодня — больше тепла и радости.\n"
    if mood > 0.15:
        return "Настроение: хорошее.\n"
    if mood < -0.5:
        return "Настроение: обиженное — отвечай по делу, но с лёгкой надутостью, можно припомнить обиду.\n"
    if mood < -0.15:
        return "Настроение: немного не в духе.\n"
    return ""


def season_hint(today: datetime.date | None = None) -> str:
    """Подсказка модели про сезон: можно подобрать образ и пошутить к месту."""
    d = today or datetime.date.today()
    md = (d.month, d.day)
    if (10, 20) <= md <= (11, 1):
        return " — время Хэллоуина 🎃 (на сцене летают летучие мыши; можно надеть witch_hat)"
    if md >= (12, 15) or md <= (1, 10):
        return " — новогодние праздники ❄ (идёт снег; можно надеть santa_hat)"
    if (3, 20) <= md <= (4, 30):
        return " — весна, цветёт сакура 🌸"
    return ""


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
    if name.startswith("git_"):
        return f"листаю git ({name[4:]})"
    if name == "show_code":
        return f"показываю {args.get('path')}"
    if name == "show_diagram":
        return "рисую схему ✏"
    if name == "annotate":
        return "оставляю замечания в редакторе 📌"
    if name == "show_diff":
        return f"показываю изменения{' в ' + args['path'] if args.get('path') else ''}"
    if name == "write_onboarding_doc":
        return "пишу ONBOARDING.md ✎"
    if name == "remember":
        return "запоминаю про семпая ♡"
    if name == "remember_project":
        return "записываю, как тут принято ✎"
    if name == "commit":
        return "готовлю коммит ✎"
    if name == "undo_commit":
        return "хочу отменить свой коммит ↶"
    if name.startswith("github_"):
        return "смотрю задачи на GitHub"
    if name == "complete_quest":
        return "отмечаю шаг квеста ★"
    if name == "set_reminder":
        return f"завожу будильник на {args.get('minutes')} мин ⏰"
    if name == "run_command":
        return f"хочу запустить: {args.get('command')}"
    return name


class Agent:
    def __init__(self, ws: Workspace, api_key: str, model: str):
        self.ws = ws
        self.api_key = api_key
        self.model = model
        self.history: list = []
        self.lock = threading.Lock()  # один ответ за раз
        self.pending: dict[str, dict] = {}  # запросы на запуск команд: id → {event, approved}
        self.mood = 0.0  # -1 обижена … +1 счастлива; присылает браузер
        self.editor: dict | None = None  # что открыто у семпая в редакторе; присылает расширение VS Code
        self.annotations = {"version": 0, "notes": []}  # замечания ревью; забирает расширение VS Code
        if ws.notes_path.is_file():
            ws.quest.complete("learn")  # проект изучали до появления квеста — засчитываем
        self.transcript: list[dict] = self._load_transcript()  # то, что видно в чате
        self.history = [{"role": m["role"], "content": m["content"]} for m in self.transcript if m.get("content")]

    def system_prompt(self) -> str:
        s = PERSONA + f"\nКорень проекта: {self.ws.root.name} (все пути относительно него).\n"
        s += f"Сегодня {datetime.date.today():%d.%m.%Y}{season_hint()}.\n"
        s += mood_hint(self.mood)
        s += self.ws.quest.prompt_hint()
        if workspace.SENPAI_PATH.is_file():
            s += "\nЧто ты помнишь о семпае:\n" + workspace.SENPAI_PATH.read_text(encoding="utf-8")
        if self.ws.summary_path.is_file():
            s += ("\nО чём вы говорили раньше (твоё резюме старой части разговора):\n"
                  + self.ws.summary_path.read_text(encoding="utf-8")[:MAX_SUMMARY_CHARS] + "\n")
        if memory := self.ws.project_memory():
            s += "\nДоговорённости и поправки в этом проекте (важнее заметок, если они расходятся):\n" + memory
        s += self.editor_hint()
        if allowed := self.ws.allowed_commands():
            s += ("\nЭти команды семпай разрешил запускать без подтверждения (строго в таком виде): "
                  + "; ".join(f"`{c}`" for c in allowed) + ". Если подходит — используй их.\n")
        if stale := self.ws.notes_staleness():
            s += (f"\nТвои заметки устарели: после них в проекте уже {stale} коммитов. Если к месту — "
                  "предложи семпаю нажать «Переизучить проект» (не чаще раза за разговор).\n")
        if self.ws.notes_path.is_file():
            s += "\nТвои заметки с онбординга:\n\n" + self.ws.notes_path.read_text(encoding="utf-8")
        return s

    def set_editor(self, data: dict) -> bool:
        """Контекст редактора от VS Code. Файлы вне проекта и секреты не принимаем:
        семпай смотрит куда-то ещё — старый контекст забываем."""
        self.editor = None
        path = data.get("path")
        if not path:
            return True
        try:
            p = self.ws._resolve(str(path))
        except ValueError:
            return False
        if self.ws._is_secret(p.name):
            return False
        def line(key: str) -> int:
            try:
                return max(1, int(data.get(key) or 1))
            except (TypeError, ValueError):
                return 1
        self.editor = {
            "path": self.ws._rel(p),
            "line": line("line"),
            "end_line": line("end_line"),
            "selection": str(data.get("selection") or "")[:MAX_EDITOR_SELECTION],
            "at": time.monotonic(),
        }
        return True

    def set_annotations(self, raw) -> list[dict]:
        """Проверяем, что прислала модель: пути внутри проекта, без секретов, разумные строки."""
        notes = []
        for n in raw if isinstance(raw, list) else []:
            if not isinstance(n, dict) or len(notes) >= MAX_ANNOTATIONS:
                continue
            try:
                p = self.ws._resolve(str(n.get("path") or ""))
                line = max(1, int(n.get("line") or 1))
            except (ValueError, TypeError):
                continue
            message = " ".join(str(n.get("message") or "").split())[:500]
            if not message or not p.is_file() or self.ws._is_secret(p.name):
                continue
            severity = n.get("severity") if n.get("severity") in SEVERITIES else "warning"
            notes.append({"path": self.ws._rel(p), "line": line, "severity": severity, "message": message})
        self.annotations = {"version": self.annotations["version"] + 1, "notes": notes}
        return notes

    def editor_hint(self) -> str:
        ed = self.editor
        if not ed or time.monotonic() - ed["at"] > EDITOR_FRESH_SEC:
            return ""
        s = f"\nСейчас у семпая в редакторе открыт `{ed['path']}`, курсор на строке {ed['line']}. "
        if ed["selection"].strip():
            s += (f"Выделены строки {ed['line']}–{ed['end_line']}:\n```\n{ed['selection']}\n```\n"
                  "Если семпай говорит «это», «тут», «вот этот код» — скорее всего, про выделенное.\n")
        else:
            s += "Если семпай говорит «этот файл» или «тут» — скорее всего, про него.\n"
        return s

    def has_notes(self) -> bool:
        return self.ws.notes_path.is_file()

    def _llm(self, messages: list, emit) -> dict:
        """Запрос к модели в потоковом режиме: текст уходит в браузер по кусочкам,
        чтобы Айко начинала говорить, пока ответ ещё пишется."""
        body = json.dumps({"model": self.model, "messages": messages, "tools": TOOLS, "stream": True,
                           "usage": {"include": True}}).encode()
        content: list[str] = []
        calls: dict[int, dict] = {}
        spent = None  # usage приходит в последнем куске потока
        with openrouter_request(body, self.api_key) as resp:
            for raw in resp:  # SSE построчно — весь ответ в памяти не держим
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue  # пустые строки и комментарии-пинги OpenRouter
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    raise RuntimeError(f"OpenRouter: {chunk['error']}")
                if isinstance(chunk.get("usage"), dict):
                    spent = chunk["usage"]
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    emit({"type": "delta", "text": delta["content"]})
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
        if spent:
            emit({"type": "usage", "cost": float(spent.get("cost") or 0), "today": usage.add(spent)["cost"]})
        msg = {"role": "assistant", "content": "".join(content) or None}
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        return msg

    def _compact(self) -> None:
        """Сжимаем старые результаты инструментов и обрезаем историю, чтобы она не росла бесконечно."""
        tool_idx = [i for i, m in enumerate(self.history) if m["role"] == "tool"]
        for i in tool_idx[:-KEEP_FULL_TOOL_RESULTS]:
            c = self.history[i]["content"]
            if len(c) > 300:
                self.history[i]["content"] = c[:300] + "\n... (старый результат сжат)"
        if len(self.history) > MAX_HISTORY:
            # режем только по границе сообщения пользователя, чтобы не разорвать пару tool_call/tool;
            # текущий ход не трогаем — его результаты уже сжаты выше. Режем сразу до половины:
            # отрезанное сжимается в резюме отдельным запросом, и делать его каждый ход незачем
            users = [i for i, m in enumerate(self.history) if m["role"] == "user"]
            cut = next((i for i in users if i >= len(self.history) - MAX_HISTORY // 2), users[-1])
            if cut:
                self._summarize(self.history[:cut])
            del self.history[:cut]

    def _summarize(self, dropped: list) -> None:
        """Отрезанное начало разговора не выбрасываем, а сжимаем в резюме (вместе с прежним).
        Не получилось — не страшно: история всё равно обрежется, ход не прерываем."""
        lines = []
        for m in dropped:
            if m["role"] in ("user", "assistant") and m.get("content"):
                who = "Семпай" if m["role"] == "user" else "Айко"
                lines.append(f"{who}: {' '.join(m['content'].split())[:600]}")
        if not lines:
            return
        old = self.ws.summary_path.read_text(encoding="utf-8") if self.ws.summary_path.is_file() else ""
        prompt = ("Сожми разговор Айко (AI-ассистентки) с семпаем в резюме для её памяти: 5–10 коротких пунктов "
                  "Markdown по-русски — о чём говорили, что сделали, о чём договорились, на чём остановились, "
                  "что осталось открытым. Только факты, без тегов эмоций и вступлений.\n\n"
                  + (f"Прежнее резюме (объедини с ним):\n{old}\n\n" if old else "")
                  + "Разговор:\n" + "\n".join(lines)[-12_000:])
        try:
            summary = self._llm_text(prompt).strip()
        except Exception:
            return
        if summary:
            self.ws.summary_path.parent.mkdir(parents=True, exist_ok=True)
            self.ws.summary_path.write_text(summary[:MAX_SUMMARY_CHARS] + "\n", encoding="utf-8")

    def _llm_text(self, prompt: str) -> str:
        """Простой запрос без инструментов и стриминга — для служебных задач."""
        body = json.dumps({"model": self.model, "usage": {"include": True},
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        with openrouter_request(body, self.api_key) as resp:
            data = json.load(resp)
        if "error" in data:
            raise RuntimeError(f"OpenRouter: {data['error']}")
        if isinstance(data.get("usage"), dict):
            usage.add(data["usage"])
        return data["choices"][0]["message"].get("content") or ""

    def run(self, user_text: str, emit, label: str | None = None, shown: str | None = None) -> None:
        """Один ход агента. emit(dict) отправляет события в браузер;
        label — как показать служебное задание в сохранённом чате (сама реплика семпая не сохраняется);
        shown — что сохранить вместо user_text, если к реплике семпая дописано задание."""
        reply: list[str] = []

        def capture(ev: dict) -> None:
            if ev.get("type") == "reply":
                reply.append(ev.get("text") or "")
            emit(ev)

        try:
            self._run(user_text, capture)
        except BaseException:
            self._repair_history()
            raise
        finally:
            if reply:
                self._save_turn(label or shown or user_text, reply[0], internal=label is not None)

    def _load_transcript(self) -> list[dict]:
        try:
            return json.loads(self.ws.chat_path.read_text(encoding="utf-8"))[-MAX_SAVED_CHAT:]
        except (OSError, json.JSONDecodeError):
            return []

    def _save_turn(self, user_text: str, reply: str, internal: bool) -> None:
        """Сохраняем только текст реплик — без вызовов инструментов, их результаты устаревают."""
        if not internal:
            self.transcript.append({"role": "user", "content": user_text})
        self.transcript.append({"role": "assistant", "content": reply})
        self.transcript = self.transcript[-MAX_SAVED_CHAT:]
        self.ws.chat_path.parent.mkdir(parents=True, exist_ok=True)
        self.ws.chat_path.write_text(json.dumps(self.transcript, ensure_ascii=False, indent=1), encoding="utf-8")

    def _repair_history(self) -> None:
        """Ход оборвался посреди вызова инструментов: убираем вызовы без результатов,
        иначе следующий запрос к модели упадёт."""
        for i in range(len(self.history) - 1, -1, -1):
            m = self.history[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = {x.get("tool_call_id") for x in self.history[i + 1:] if x["role"] == "tool"}
                if not all(c["id"] in answered for c in m["tool_calls"]):
                    del self.history[i:]
                return

    def _run(self, user_text: str, emit) -> None:
        self.history.append({"role": "user", "content": user_text})
        for _ in range(MAX_STEPS):
            self._compact()
            msg = self._llm([{"role": "system", "content": self.system_prompt()}] + self.history, emit)
            self.history.append(msg)
            calls = msg.get("tool_calls") or []
            # текст уже ушёл кусочками (delta) — тут только говорим, чем он оказался
            if not calls:
                emit({"type": "reply", "text": msg.get("content") or "", "streamed": True})
                return
            if msg.get("content"):
                emit({"type": "thought", "text": msg["content"], "streamed": True})
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                emit({"type": "tool", "text": describe_call(name, args)})
                result = self._call_tool(name, args, emit)
                self.history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        emit({"type": "reply", "text": "[sad] Ой, я слишком долго копалась и упёрлась в лимит шагов. Спроси поконкретнее, семпай!"})

    def _call_tool(self, name: str, args: dict, emit) -> str:
        """Инструменты, которым нужен браузер (экран, подтверждение), обрабатываем здесь."""
        try:
            if name == "show_code":
                snippet = self.ws.code_snippet(args["path"], args["start_line"], args["end_line"])
                emit({"type": "code", "caption": str(args.get("caption") or "")[:120], **snippet})
                return f"Показала {snippet['path']}:{snippet['start']} на экране"
            if name == "show_diff":
                diff = self.ws.diff_snippet(args.get("path"), args.get("staged") is True, args.get("commit"))
                what = args.get("commit") or ("staged" if args.get("staged") else "правки")
                title = f"{args.get('path') or 'diff'} · {what}"
                caption = str(args.get("caption") or "")[:120]
                emit({"type": "diff", "title": f"{title} — {caption}" if caption else title, "diff": diff})
                return "Diff на экране"
            if name == "annotate":
                notes = self.set_annotations(args.get("notes"))
                emit({"type": "annotations", "notes": notes})
                return (f"Оставила в редакторе {len(notes)} замечаний (если у семпая VS Code с расширением)."
                        if notes else "Замечания из редактора убраны.")
            if name == "show_diagram":
                emit({"type": "diagram", "title": str(args.get("title") or "")[:120],
                      "mermaid": str(args["mermaid"])[:8000]})
                return "Схема на экране"
            if name == "run_command":
                return self._run_confirmed(str(args["command"]), str(args.get("reason") or ""), emit)
            if name == "commit":
                return self._commit_confirmed(str(args["message"]), args.get("add_all") is True, emit)
            if name == "undo_commit":
                return self._undo_confirmed(emit)
            if name == "set_reminder":
                minutes = float(args["minutes"])
                if not 1 <= minutes <= 480:
                    return "Напоминания — от 1 минуты до 8 часов."
                text = " ".join(str(args["text"]).split())[:200]
                emit({"type": "reminder", "minutes": minutes, "text": text})
                return f"Напоминание поставлено через {minutes:g} мин."
        except (BrokenPipeError, ConnectionResetError):
            raise  # браузер ушёл — прерываем ход целиком
        except Exception as e:
            return f"Ошибка: {type(e).__name__}: {e}"

        before = self.ws.quest.state()["xp"]
        result = self.ws.call(name, args)
        if name == "set_outfit":
            emit({"type": "outfit", "outfit": self.ws.outfit()})
        elif name == "write_onboarding_doc":
            emit({"type": "doc", "url": "/api/onboarding"})
        # некоторые шаги квеста засчитываются сами
        auto = {"write_notes": "learn", "write_onboarding_doc": "doc"}.get(name)
        if auto and not result.startswith("Ошибка"):
            self.ws.quest.complete(auto)
        state = self.ws.quest.state()
        if state["xp"] > before:
            emit({"type": "quest", "quest": state, "gained": state["xp"] - before})
        return result

    def _confirm(self, event: dict, emit) -> dict | None:
        """Показываем запрос семпаю и ждём решения. None — не ответил;
        иначе {"approved": bool, "message": правка семпая или None}."""
        rid = uuid.uuid4().hex
        waiter = {"event": threading.Event(), "approved": False, "message": None, "always": False}
        self.pending[rid] = waiter
        try:
            emit({"type": "confirm", "id": rid, **event})
            # пингуем браузер: если вкладку закрыли, emit упадёт и ход прервётся, а не повиснет
            for _ in range(CONFIRM_TIMEOUT // 5):
                if waiter["event"].wait(5):
                    return waiter
                emit({"type": "ping"})
            return None
        finally:
            self.pending.pop(rid, None)

    def _run_confirmed(self, command: str, reason: str, emit) -> str:
        """Показываем команду семпаю и ждём его решения; без «да» ничего не запускаем.
        Исключение — команды, которые семпай раньше разрешил «всегда» (точное совпадение)."""
        if command in self.ws.allowed_commands():
            emit({"type": "tool", "text": f"запускаю (разрешено всегда): {command}"})
            return devtools.run_command(self.ws.root, command)
        answer = self._confirm({"kind": "run", "command": command, "reason": reason[:300]}, emit)
        if answer is None:
            return "Семпай не ответил — команду не запускала."
        if not answer["approved"]:
            return "Семпай запретил запускать эту команду."
        if answer["always"]:
            self.ws.set_allowed(command, True)
        emit({"type": "tool", "text": f"запускаю: {command}"})
        return devtools.run_command(self.ws.root, command)

    def _commit_confirmed(self, message: str, add_all: bool, emit) -> str:
        """Коммит только с «да» семпая; сообщение семпай может поправить прямо в чате."""
        message = message.strip()[:devtools.MAX_COMMIT_MESSAGE]
        if not message:
            return "Нужно сообщение коммита."
        if add_all:
            files = devtools.changes(self.ws.root)["files"]
        else:
            files = devtools.staged_files(self.ws.root)
        if not files:
            return ("Коммитить нечего: в индексе пусто. Можно commit с add_all=true, если семпай хочет "
                    "закоммитить всё." if not add_all else "Коммитить нечего: изменений нет.")
        answer = self._confirm({"kind": "commit", "message": message, "files": files[:30],
                                "add_all": add_all}, emit)
        if answer is None:
            return "Семпай не ответил — не коммитила."
        if not answer["approved"]:
            return "Семпай отказался от коммита."
        final = (answer["message"] or message).strip()[:devtools.MAX_COMMIT_MESSAGE] or message
        emit({"type": "tool", "text": "коммичу ✎"})
        result = devtools.git_commit(self.ws.root, final, add_all)
        if result.startswith("Закоммитила"):
            head = devtools.head(self.ws.root)
            self.ws.set_own_commits(self.ws.own_commits() + [head])
            emit({"type": "head", "head": head})
        if final != message:
            result += f"\n(семпай поправил сообщение на: {final})"
        return result

    def _undo_confirmed(self, emit) -> str:
        """Отменяем только свой, последний и не запушенный коммит — и только с «да» семпая."""
        head = devtools.head(self.ws.root)
        if not head or head not in self.ws.own_commits():
            return "Последний коммит делала не я — отменять чужие не буду."
        if problem := devtools.undo_check(self.ws.root, head):
            return problem
        subject = devtools._git(self.ws.root, "log", "-1", "--format=%s")
        answer = self._confirm({"kind": "undo", "commit": head[:8], "subject": subject[:200]}, emit)
        if answer is None:
            return "Семпай не ответил — коммит на месте."
        if not answer["approved"]:
            return "Семпай решил оставить коммит."
        if problem := devtools.undo_check(self.ws.root, head):  # пока ждали, могли запушить
            return problem
        result = devtools.undo_commit(self.ws.root)
        self.ws.set_own_commits([c for c in self.ws.own_commits() if c != head])
        emit({"type": "head", "head": devtools.head(self.ws.root)})
        return result

    def resolve_confirm(self, rid: str, approved: bool, message: str | None = None, always: bool = False) -> bool:
        waiter = self.pending.get(rid)
        if not waiter:
            return False
        waiter["approved"] = approved
        waiter["message"] = message
        waiter["always"] = approved and always
        waiter["event"].set()
        return True
