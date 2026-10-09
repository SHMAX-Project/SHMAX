#!/usr/bin/env bash
# Separate, root-owned host runtime survives /opt/shmax replacement.
set -euo pipefail
source_root="$1"
runtime="${2:-/opt/shmax-hostd}"
python="${SHMAX_HOST_PYTHON:-python3}"
"$python" -c 'import sys; assert sys.version_info >= (3, 12), "Python >=3.12 required"'
install -d -m 0755 "$runtime"
if [[ ! -x "$runtime/.venv/bin/python" ]]; then
    "$python" -m venv "$runtime/.venv"
fi
"$runtime/.venv/bin/python" -m pip install -r "$source_root/deploy/hostd/requirements.txt"
install -d -m 0755 "$runtime/lib" "$runtime/bin"
# Non-editable copy: no source checkout or its .venv is used at runtime.
rm -rf "$runtime/lib/shmax" "$runtime/lib/alembic"
cp -R "$source_root/backend/shmax" "$runtime/lib/shmax"
cp -R "$source_root/backend/alembic" "$runtime/lib/alembic"
cp "$source_root/backend/alembic.ini" "$runtime/lib/alembic.ini"
cat > "$runtime/bin/shmax" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
launcher="$0"
if [[ -L "$launcher" ]]; then launcher="$(readlink "$launcher")"; fi
runtime="$(cd "$(dirname "$launcher")/.." && pwd)"
export PYTHONPATH="$runtime/lib"
exec "$runtime/.venv/bin/python" -P -m shmax.cli "$@"
WRAPPER
chmod 0755 "$runtime/bin/shmax"
# Prove both daemon and the fixed CLI commands import before systemd installation.
PYTHONPATH="$runtime/lib" "$runtime/.venv/bin/python" -P -c \
    'import shmax.host.server, shmax.host.updater, shmax.backup.manager, shmax.cli.main'
"$runtime/bin/shmax" version
