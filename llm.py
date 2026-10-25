"""
Внешние сервисы Айко: OpenRouter (мозги и распознавание речи) и edge-tts (озвучка).
"""

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request

try:  # голос: нейросетевой TTS Microsoft Edge (онлайн, локально ничего не грузит)
    import edge_tts
except ImportError:
    edge_tts = None

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"

# распознавание речи: дешёвая модель OpenRouter, которая понимает аудио (меняется в .env)
STT_MODEL = "google/gemini-2.5-flash-lite"
STT_FALLBACKS = ["google/gemini-3-flash-preview", "qwen/qwen3.8-omni-flash"]  # если основную ограничили

# голос можно поменять в .env
TTS_VOICE = "ru-RU-SvetlanaNeural"
TTS_PITCH = "+30Hz"  # повыше — по-анимешному
TTS_RATE = "+8%"

MAX_TTS_CHARS = 600         # озвучиваем по предложению, длиннее не нужно
MAX_AUDIO_BYTES = 1_500_000  # ~45 секунд речи в WAV 16 кГц моно


def openrouter_request(body: bytes, api_key: str):
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Aiko-chan onboarding",
    })
    try:
        return urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {e.read(2000).decode(errors='replace')}") from e


def transcribe(wav: bytes, api_key: str) -> str:
    """Речь семпая → текст через модель OpenRouter, которая понимает аудио."""
    body = json.dumps({
        # OpenRouter сам переключится на следующую модель, если предыдущая недоступна
        "models": list(dict.fromkeys([os.environ.get("AIKO_STT_MODEL", STT_MODEL), *STT_FALLBACKS])),
        "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Дословно запиши, что сказано в аудио, на языке оригинала "
                                     "(обычно русский). Только текст, без пояснений и кавычек. "
                                     "Если речи нет или неразборчиво — верни пустую строку."},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}},
        ]}],
    }).encode()
    with openrouter_request(body, api_key) as resp:
        data = json.load(resp)
    if "error" in data:
        raise RuntimeError(f"OpenRouter: {data['error']}")
    return (data["choices"][0]["message"].get("content") or "").strip()


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
