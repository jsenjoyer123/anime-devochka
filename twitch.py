"""
Чат Twitch для Айко: анонимное чтение по IRC (justinfan — ключ не нужен), только чтение.
Последние сообщения держим в памяти; на лучшие она отвечает голосом на стриме.
"""

import collections
import random
import re
import socket
import threading
import time

HOST, PORT = "irc.chat.twitch.tv", 6667
MAX_MESSAGES = 50
MAX_TEXT = 300
RECONNECT_MAX_S = 60
CHANNEL_RE = re.compile(r"^[a-z0-9_]{3,25}$")
PRIVMSG_RE = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<nick>[^!\s]+)!\S+ PRIVMSG #\S+ :(?P<text>.*)$")


def parse(line: str) -> dict | None:
    """Строка IRC → {user, text} для сообщений чата, иначе None."""
    m = PRIVMSG_RE.match(line)
    if not m:
        return None
    tags = dict(t.split("=", 1) for t in (m["tags"] or "").split(";") if "=" in t)
    user = tags.get("display-name") or m["nick"]
    text = " ".join(m["text"].replace("\x01ACTION", "").replace("\x01", "").split())[:MAX_TEXT]
    return {"user": user[:25], "text": text} if text else None


class TwitchChat:
    def __init__(self, channel: str, host: str = HOST, port: int = PORT):
        channel = channel.strip().lstrip("#").lower()
        if not CHANNEL_RE.match(channel):
            raise ValueError(f"Некорректное имя канала Twitch: {channel!r}")
        self.channel, self.host, self.port = channel, host, port
        self.messages: collections.deque = collections.deque(maxlen=MAX_MESSAGES)
        self.seq = 0         # номер последнего сообщения
        self.answered = 0    # до какого номера Айко уже ответила
        self.connected = False
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> "TwitchChat":
        threading.Thread(target=self._loop, name="twitch", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def since(self, seq: int) -> list[dict]:
        with self.lock:
            return [m for m in self.messages if m["id"] > seq]

    def take_unanswered(self, limit: int = 15) -> list[dict]:
        """Ещё не отвеченные сообщения (последние limit); дальше считаем их отвеченными."""
        with self.lock:
            fresh = [m for m in self.messages if m["id"] > self.answered][-limit:]
            self.answered = self.seq
            return fresh

    def _add(self, msg: dict) -> None:
        with self.lock:
            self.seq += 1
            self.messages.append({"id": self.seq, "at": time.time(), **msg})

    def _loop(self) -> None:
        delay = 1
        while not self._stop.is_set():
            try:
                self._session()
                delay = 1
            except OSError:
                pass
            self.connected = False
            self._stop.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    def _session(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            sock.settimeout(360)  # Twitch шлёт PING примерно раз в 5 минут
            nick = f"justinfan{random.randint(10_000, 99_999)}"
            sock.sendall(f"CAP REQ :twitch.tv/tags\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
            self.connected = True
            buf = b""
            while not self._stop.is_set():
                data = sock.recv(4096)
                if not data:
                    return
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace")
                    if line.startswith("PING"):
                        sock.sendall(("PONG" + line[4:] + "\r\n").encode())
                    elif msg := parse(line):
                        self._add(msg)
