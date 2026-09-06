#!/data/data/com.termux/files/usr/bin/env bash
#
# Installation du bot sur Termux (Android/ARM).
#
#   bash scripts/termux_setup.sh          # bot seul, aucune compilation
#   bash scripts/termux_setup.sh --llm    # + couche d'analyse (compile Rust une fois)
#   bash scripts/termux_setup.sh --server # + serveur Pokemon Showdown local (Node)
#
# Pourquoi ce script plutot qu'un simple `pip install -r requirements.txt`:
# pip sur Termux refuse les wheels manylinux (Android utilise bionic, pas glibc),
# donc tout paquet distribue uniquement en manylinux se compile depuis les sources.
# On evite ces compilations pour le bot, et on met en cache celles de la couche
# d'analyse pour ne les subir qu'une seule fois.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEELHOUSE="${HOME}/.cache/showdownbot-wheels"
WITH_LLM=false
WITH_SERVER=false

for arg in "$@"; do
  case "$arg" in
    --llm) WITH_LLM=true ;;
    --server) WITH_SERVER=true ;;
    --all) WITH_LLM=true; WITH_SERVER=true ;;
    *) echo "Option inconnue: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Paquets systeme"
pkg install -y python git tmux

say "Environnement virtuel"
cd "$ROOT"
[ -d .venv ] || python -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel

say "Dependances du bot (aucune extension native)"
# --no-deps sur poke-env: ses dependances numpy/gymnasium/pettingzoo ne servent
# qu'a poke_env.environment (RL), que ce projet n'importe jamais, et son orjson
# est remplace par compat/orjson.py.
pip install --no-deps "poke-env==0.16.1"
pip install "websockets==16.1.1" "requests>=2.31" "tabulate>=0.9" "python-dotenv>=1.0" "Flask>=3.0"

say "Verification de l'installation du bot"
python - <<'PY'
from bot.compat import ensure_orjson, is_termux
shim = ensure_orjson()
import poke_env
from poke_env.calc import calculate_damage  # noqa: F401
print("poke-env         :", poke_env.__name__, "importe sans numpy")
print("shim orjson      :", "actif (stdlib)" if shim else "inutile (orjson present)")
print("plateforme Termux:", is_termux())
PY

if [ "$WITH_LLM" = true ]; then
  say "Couche d'analyse LLM"
  if [ -d "$WHEELHOUSE" ] && ls "$WHEELHOUSE"/*.whl >/dev/null 2>&1; then
    echo "Wheels en cache trouves dans $WHEELHOUSE, aucune recompilation."
  else
    echo "Premiere installation: compilation de pydantic-core et jiter (Rust)."
    echo "Compter 10 a 30 minutes selon le telephone. Une seule fois."
    pkg install -y rust binutils
    mkdir -p "$WHEELHOUSE"
    # CARGO_BUILD_JOBS=1 evite les OOM sur les telephones a faible RAM.
    CARGO_BUILD_JOBS=1 pip wheel "anthropic>=1.4.0,<2" -w "$WHEELHOUSE"
  fi
  pip install --find-links "$WHEELHOUSE" "anthropic>=1.4.0,<2"
  python -c "import anthropic; print('anthropic', anthropic.__version__, 'installe')"
fi

if [ "$WITH_SERVER" = true ]; then
  say "Serveur Pokemon Showdown local"
  pkg install -y nodejs-lts
  SERVER_DIR="${HOME}/pokemon-showdown"
  if [ ! -d "$SERVER_DIR" ]; then
    echo "Clone en cours (~500 Mo une fois les dependances installees)."
    git clone --depth 1 https://github.com/smogon/pokemon-showdown.git "$SERVER_DIR"
  fi
  cd "$SERVER_DIR"
  npm install --omit=dev
  [ -f config/config.js ] || cp config/config-example.js config/config.js
  # --no-security: pas d'authentification, indispensable pour le bot-vs-bot local.
  echo "Lancer le serveur avec: node $SERVER_DIR/pokemon-showdown start --no-security"
  cd "$ROOT"
fi

say "Configuration"
[ -f .env ] || { cp .env.example .env; echo "Cree: .env (a completer)"; }

say "Termine"
cat <<'EOF'
Activer l'environnement:   source .venv/bin/activate
Lancer un combat local:    python -m bot.cli selfplay --battles 3
Lancer le dashboard:       python -m web.app
Doc complete:              README.md et docs/termux.md
EOF
