#!/usr/bin/env bash
# Прогнать пайплайн CI (.github/workflows/tests.yml) локально, как на чистом раннере:
# копия файлов из git (без .env, notes/, .venv), временный HOME без git-конфига,
# Python 3.10 и 3.12, Node той же версии, что в CI, свежий Playwright без кэша CDN.
#
#   scripts/ci-local.sh          # рабочая копия (с незакоммиченными правками)
#   scripts/ci-local.sh HEAD     # ровно то, что закоммичено
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE_VERSION="$(grep -A2 'setup-node' "$ROOT/.github/workflows/tests.yml" | grep -oE 'node-version: *"?[0-9]+' | grep -oE '[0-9]+$')"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SRC="$WORK/src"
mkdir -p "$SRC" "$WORK/home"

if [ "${1:-}" = "HEAD" ]; then
  git -C "$ROOT" archive HEAD | tar -x -C "$SRC"
else
  (cd "$ROOT" && git ls-files -co --exclude-standard -z | xargs -0 cp --parents -t "$SRC")
fi

# как на раннере: нет глобального git-конфига, своего HOME и кэшей
export HOME="$WORK/home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
unset OPENROUTER_API_KEY GITHUB_TOKEN TWITCH_CHANNEL AIKO_CHROMIUM
REAL_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
export PLAYWRIGHT_BROWSERS_PATH="$REAL_HOME/.cache/ms-playwright"  # браузер качаем один раз, остальное — с нуля
export UV_CACHE_DIR="$REAL_HOME/.cache/uv"

results=()
step() {  # step "имя" команда...
  local name="$1"; shift
  echo "::group::$name"
  if (cd "$SRC" && "$@"); then results+=("✅ $name"); else results+=("❌ $name"); fi
  echo "::endgroup::"
}

for py in 3.10 3.12; do
  step "tests ($py): ruff" uvx --quiet ruff check .
  step "tests ($py): unittest" uv run --quiet --no-project --python "$py" python -m unittest discover -s tests
done

NODE_DIR="$REAL_HOME/.cache/aiko-ci-node/$NODE_VERSION"
if [ ! -x "$NODE_DIR/bin/node" ]; then
  mkdir -p "$NODE_DIR"
  ver="$(curl -fsSL https://nodejs.org/dist/index.json | python3 -c "import json,sys; print(next(r['version'] for r in json.load(sys.stdin) if r['version'].startswith('v$NODE_VERSION.')))")"
  curl -fsSL "https://nodejs.org/dist/$ver/node-$ver-linux-x64.tar.xz" | tar -xJ -C "$NODE_DIR" --strip-components=1
fi
step "vscode (node $("$NODE_DIR/bin/node" --version))" sh -c "\"$NODE_DIR/bin/node\" --test tests/vscode/*.test.js"

export AIKO_E2E_CACHE="$WORK/cdn-cache"  # кэш CDN пустой, как на раннере
step "e2e" uv run --quiet --no-project --python 3.12 --with playwright \
  sh -c 'python -m playwright install chromium >/dev/null && python -m unittest discover -s tests/e2e'

echo; printf '%s\n' "${results[@]}"
! printf '%s\n' "${results[@]}" | grep -q '❌'
