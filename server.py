#!/usr/bin/env python3
"""
Бэкенд Айко-тян: отдаёт веб-страницу с 3D-моделью и крутит агента,
который изучает кодовую базу через OpenRouter.

Запуск:  .venv/bin/python server.py /путь/к/проекту [--port 8765]

Устройство: workspace.py — песочница файлов и память, agent.py — персона и цикл
tool calling, llm.py — OpenRouter и edge-tts, http_api.py — HTTP и API.
"""

import argparse
import os
from http.server import ThreadingHTTPServer
from pathlib import Path

import http_api
from llm import DEFAULT_MODEL
from twitch import TwitchChat

HERE = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
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

    http_api.API_KEY = api_key
    http_api.MODEL = model = args.model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL
    http_api.open_project(root)
    if channel := os.environ.get("TWITCH_CHANNEL", "").strip():
        try:
            http_api.TWITCH = TwitchChat(channel).start()
        except ValueError as e:
            print(e)
            return 1

    # слушаем только localhost: наружу проект не светим
    server = ThreadingHTTPServer(("127.0.0.1", args.port), http_api.Handler)
    server.daemon_threads = True
    print(f"✿ Айко-тян ждёт тебя: http://127.0.0.1:{args.port}")
    print(f"  проект: {root}\n  модель: {model}")
    if http_api.TWITCH:
        print(f"  чат Twitch: #{http_api.TWITCH.channel} (только чтение)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nПока-пока, семпай!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
