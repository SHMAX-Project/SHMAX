#!/usr/bin/env bash
# Pinned official scanner, checksum verified; never prints secret values.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
VERSION=8.30.1
case "$(uname -s)/$(uname -m)" in
 Linux/x86_64) PLATFORM=linux_x64;;
 Darwin/arm64) PLATFORM=darwin_arm64;;
 *) echo 'unsupported scanner platform' >&2; exit 1;;
esac
BASE="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}"
ARCHIVE="gitleaks_${VERSION}_${PLATFORM}.tar.gz"
curl --fail --silent --show-error --location "$BASE/$ARCHIVE" -o "$STAGE/$ARCHIVE"
curl --fail --silent --show-error --location "$BASE/gitleaks_${VERSION}_checksums.txt" -o "$STAGE/checksums"
python3 - "$STAGE" "$ARCHIVE" <<'PY'
import hashlib,pathlib,sys,tarfile
stage,name=pathlib.Path(sys.argv[1]),sys.argv[2]
expected=next(l.split()[0] for l in (stage/'checksums').read_text().splitlines() if l.split()[-1]==name)
assert hashlib.sha256((stage/name).read_bytes()).hexdigest()==expected
with tarfile.open(stage/name) as t:t.extract('gitleaks',stage,filter='data')
PY
cd "$ROOT"
REVIEWED="release/v$(cat backend/shmax/VERSION)/gitleaks-reviewed-fixtures.ignore"
[[ -f "$REVIEWED" ]] || REVIEWED="release/v0.1.0/gitleaks-reviewed-fixtures.ignore"
# Isolated test fixtures are reviewed individually in the redacted baseline.
# Only exact reviewed fingerprints are allowed; arbitrary credentials fail closed.
"$STAGE/gitleaks" git --redact=100 --no-banner --log-opts=--all \
 --gitleaks-ignore-path "$REVIEWED" \
 --report-format json --report-path "$STAGE/report.json" > "$STAGE/log" 2>&1 && CODE=0 || CODE=$?
python3 - "$STAGE/report.json" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1]);findings=json.loads(p.read_text()) if p.exists() else []
for f in findings:print(f"{f['File']}:{f['StartLine']} category={f['RuleID']} commit={f.get('Commit','working-tree')}")
print(f"Unreviewed secret candidates: {len(findings)}")
PY
exit "$CODE"
