#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build only committed files; never consume working frontend/dist, node_modules,
# .venv or .git. No tag creation, network publication or GitHub writes.
set -euo pipefail
TAG="${1:?usage: build-release.sh vX.Y.Z [output-directory]}"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.-]+)?$ ]] || { echo 'invalid version' >&2; exit 1; }
VERSION="${TAG#v}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${2:-${ROOT}/dist-release}"
[[ -z "$(git -C "$ROOT" status --porcelain)" ]] || { echo 'committed clean source required' >&2; exit 1; }
[[ "$(git -C "$ROOT" show HEAD:backend/shmax/VERSION)" == "$VERSION" ]] || { echo 'VERSION mismatch' >&2; exit 1; }
HEAD="$(git -C "$ROOT" rev-parse HEAD)"
EPOCH="$(git -C "$ROOT" show -s --format=%ct HEAD)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$OUT" "$STAGE/source"
OUT="$(cd "$OUT" && pwd)"
git -C "$ROOT" archive --format=tar --prefix="shmax-${TAG}/" HEAD | tar -xf - -C "$STAGE/source"
# Build the frontend in the isolated exported source, with the committed npm lock.
( cd "$STAGE/source/shmax-${TAG}/frontend" && npm ci --no-audit --no-fund && npm run build )
python3 - "$TAG" "$OUT" "$STAGE/source" "$HEAD" "$EPOCH" <<'PY'
import datetime, gzip, hashlib, json, pathlib, subprocess, sys, tarfile
tag, out, source, head, epoch = sys.argv[1:]
out, source, epoch = pathlib.Path(out), pathlib.Path(source), int(epoch)
root = source / f"shmax-{tag}"
version = tag[1:]
files = []
for p in sorted(root.rglob("*")):
    rel = p.relative_to(root)
    if any(part in {"node_modules", ".git", ".venv", "__pycache__"} for part in rel.parts):
        continue
    if p.name.endswith(".tsbuildinfo") or p.is_dir():
        continue
    if p.is_symlink():
        raise SystemExit("unexpected source symlink")
    files.append(p)
artifacts = []
for name, production in [(f"shmax-{tag}-source.tar.gz", False), (f"shmax-{tag}.tar.gz", True)]:
    with (out / name).open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for p in files:
            rel = p.relative_to(root)
            if not production and rel.parts[:2] == ("frontend", "dist"):
                continue
            info = tar.gettarinfo(str(p), arcname=str(pathlib.Path(root.name) / rel))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = epoch
            info.mode = 0o755 if p.stat().st_mode & 0o111 else 0o644
            info.pax_headers = {}
            with p.open("rb") as stream:
                tar.addfile(info, stream)
    archive = out / name
    artifacts.append({"name": name, "size": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
production = artifacts[1]
manifest = {
    "version": tag,
    "channel": "beta" if "-" in version else "stable",
    "artifact": production["name"],
    "sha256": production["sha256"],
}
(out / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
(out / "SHA256SUMS").write_text("".join(f"{a['sha256']}  {a['name']}\n" for a in artifacts))
info = {"source_head": head, "source_date_epoch": epoch, "build_date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "artifacts": artifacts, "dependencies": {"python": ">=3.12", "node": ">=20", "frontend_lock": "frontend/package-lock.json", "backend_lock": "backend/requirements.lock", "installation": "Debian 13 amd64, Docker, systemd"}}
info["toolchain"] = {"python": sys.version.split()[0], "node": subprocess.check_output(["node", "--version"], text=True).strip(), "npm": subprocess.check_output(["npm", "--version"], text=True).strip()}
(out / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")
print(json.dumps(info, indent=2))
PY
