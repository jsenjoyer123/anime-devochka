"""Упавшие тесты из лога unittest → аннотации GitHub Actions (::error::).

Логи CI без прав администратора не скачать, а аннотации видны через публичный API —
так причину падения видно сразу. Запуск: python tests/ci_annotate.py test.log
"""

import re
import sys


def esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: str) -> None:
    log = open(path, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)\n-+\n(.*?)(?=^={20,}|^-{20,}\nRan )", log, re.M | re.S):
        kind, test, where, body = m.groups()
        lines = [x for x in body.strip().splitlines() if x.strip()]
        # самое полезное — строка исключения и пара строк вокруг неё
        tail = lines[-6:]
        name = where if where.endswith(test) else f"{where}.{test}"  # 3.11+ пишет в скобках полный id
        print(f"::error title={kind} {name}::{esc(chr(10).join(tail))}")


if __name__ == "__main__":
    main(sys.argv[1])
