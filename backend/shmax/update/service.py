"""GitHub Releases update discovery (live acceptance #6-#9, #14).

Boundary design:

* This service ONLY discovers releases and publishes update
  metadata. Download / verify / install / rollback belong to the
  host-side ``shmax-hostd`` updater — the web app never touches
  Docker or /opt/shmax.
* The GitHub boundary is a single pair of injectable callables
  (``http_json`` / ``http_text``); tests mock THAT boundary only.
* The production transport is aiohttp with a bounded timeout and a
  hard response size cap; errors are sanitized — URLs and tokens
  never appear in exception text.
* No GitHub token is required for public releases; an optional
  token is read from the SecretStore and never logged.
* Startup never blocks on this service and never fails because of
  it: the 24h auto-check is a best-effort background task.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

GITHUB_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MiB hard cap
AUTO_CHECK_INTERVAL_SECONDS = 24 * 3600
FIRST_AUTO_CHECK_DELAY_SECONDS = 300
STATE_KEY = "update_check_state"
CHANNEL_KEY = "update_channel"
REPO_FILE_SUBPATH = "state/update_repo"

SUPPORTED_CHANNELS = ("stable", "beta")

_SEMVER_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$"
)


# Live acceptance A4: STRICT trust boundary. GitHub is the ONLY
# release source; API calls go to exactly one origin (the only one
# that may carry the optional token); release asset downloads are
# allowlisted to GitHub's real release hosts; every redirect hop is
# re-validated; HTTP downgrades and loopback/private targets never
# pass.
GITHUB_API_ORIGIN = "https://api.github.com"
GITHUB_WEB_ORIGIN = "https://github.com"
GITHUB_ASSET_ORIGINS = (
    "https://objects.githubusercontent.com",
    "https://release-assets.githubusercontent.com",
)
_ALLOWED_REDIRECT_HOPS = 5

_REPO_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})"
    "/([A-Za-z0-9._-]{1,100})/?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateCheckError(Exception):
    """Sanitized check failure (see ``status`` for the machine kind)."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class ReleaseMeta:
    version: str
    channel: str
    notes: str
    published_at: str
    artifact_url: str
    artifact_name: str
    sha256: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "channel": self.channel,
            "notes": self.notes,
            "published_at": self.published_at,
            "artifact_url": self.artifact_url,
            "artifact_name": self.artifact_name,
            "sha256": self.sha256,
        }


def parse_version(v: str) -> Optional[tuple[int, int, int, str]]:
    m = _SEMVER_RE.match((v or "").strip())
    if m is None:
        return None
    pre = m.group(4) or ""
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)


def is_prerelease(v: str) -> bool:
    parsed = parse_version(v)
    return bool(parsed and parsed[3])


def _cmp_versions(a: str, b: str) -> int:
    """Semantic Versioning 2.0.0 precedence (live acceptance A3).

    1.0.0-rc.1 < 1.0.0; 0.2.0-beta.2 < 0.2.0; 0.2.0-alpha <
    0.2.0-beta; numeric identifiers compare numerically
    (beta.9 < beta.10); build metadata is ignored.
    """
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return 0
    if pa[:3] != pb[:3]:
        return -1 if pa[:3] < pb[:3] else 1
    # Same core: a version WITHOUT prerelease outranks one WITH.
    if not pa[3] and not pb[3]:
        return 0
    if not pa[3]:
        return 1
    if not pb[3]:
        return -1

    def _pre_keys(pre: str) -> list:
        keys: list = []
        for part in pre.split("."):
            if part.isdigit():
                keys.append((0, int(part), ""))
            else:
                keys.append((1, 0, part))
        return keys

    ka, kb = _pre_keys(pa[3]), _pre_keys(pb[3])
    # A larger set of fields has higher precedence when all
    # preceding identifiers are equal (beta < beta.1).
    for x, y in zip(ka, kb):
        if x != y:
            return -1 if x < y else 1
    return (len(ka) > len(kb)) - (len(ka) < len(kb))


def parse_repo_url(repo_url: str) -> Optional[str]:
    """Validate STRICTLY and map to the GitHub API releases URL.

    Accepts ONLY ``https://github.com/owner/repository`` shapes
    (live acceptance A4): https, github.com, exactly two path
    segments, no userinfo, no port, no query/fragment, no
    traversal-like segments. Everything else — http://, file://,
    raw IPs, localhost, nested paths — returns None (the caller
    surfaces ``not_configured``)."""
    m = _REPO_URL_RE.match((repo_url or "").strip())
    if m is None:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo in (".", "..") or ".." in owner or ".." in repo:
        return None
    if "." in repo and repo.endswith((".git",)):
        repo = repo[:-4]
    return f"{GITHUB_API_ORIGIN}/repos/{owner}/{repo}/releases"


def assert_url_allowed(url: str, *, token_origin: bool = False) -> None:
    """Raise UpdateCheckError('network_error') unless ``url`` is an
    HTTPS origin on the GitHub allowlist (live acceptance A4)."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise UpdateCheckError("network_error", "bad url") from e
    if parts.scheme != "https":
        raise UpdateCheckError("network_error", "non-https origin blocked")
    if parts.username or parts.password:
        raise UpdateCheckError("network_error", "userinfo blocked")
    if parts.port is not None:
        raise UpdateCheckError("network_error", "non-default port blocked")
    host = (parts.hostname or "").lower()
    if host in ("localhost",) or host.endswith(".localhost"):
        raise UpdateCheckError("network_error", "loopback blocked")
    import ipaddress as _ip

    try:
        ip = _ip.ip_address(host)
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise UpdateCheckError("network_error", "private address blocked")
        raise UpdateCheckError("network_error", "origin not allowlisted")
    except ValueError:
        pass  # not a literal IP — hostname allowlist below
    allowed = (GITHUB_API_ORIGIN, GITHUB_WEB_ORIGIN, *GITHUB_ASSET_ORIGINS)
    if not any(url == o or url.startswith(o + "/") for o in allowed):
        raise UpdateCheckError("network_error", "origin not allowlisted")
    if token_origin and not (
        url == GITHUB_API_ORIGIN or url.startswith(GITHUB_API_ORIGIN + "/")
    ):
        raise UpdateCheckError("network_error", "token origin mismatch")


def normalize_sha256(value: str) -> str:
    """Live acceptance A5: exactly 64 hex chars, lowercase."""
    v = (value or "").strip().lower()
    if not _SHA256_RE.match(v):
        raise UpdateCheckError("bad_manifest", "invalid sha256")
    return v


def verify_artifact_bytes(data: bytes, expected_sha256: str) -> None:
    """Live acceptance A5: the DOWNLOADED bytes must hash to the
    trusted expected value BEFORE anything is staged/installed."""
    import hashlib as _hl

    actual = _hl.sha256(data).hexdigest()
    if actual != normalize_sha256(expected_sha256):
        raise UpdateCheckError("sha_mismatch", "artifact hash mismatch")


# ----------------------------------------------------------------------
# Production HTTP boundary (aiohttp, bounded, sanitized)
# ----------------------------------------------------------------------
async def http_json(url: str, token: str = "") -> Any:
    raw = await _http_get(url, token)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise UpdateCheckError("bad_manifest", f"invalid JSON ({type(e).__name__})") from e


async def http_text(url: str, token: str = "") -> str:
    raw = await _http_get(url, token)
    return raw.decode("utf-8", errors="replace")


async def _http_get(url: str, token: str = "") -> bytes:
    """Bounded, redirect-validated GET (live acceptance A4).

    Redirects are followed MANUALLY: every hop must stay HTTPS on
    the GitHub allowlist (api.github.com → github.com →
    *githubusercontent.com asset hosts). The optional token is sent
    ONLY to the API origin and never re-attached to a redirect
    target; nothing is logged."""
    import asyncio

    import aiohttp

    assert_url_allowed(url)
    current = url
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=GITHUB_TIMEOUT_SECONDS),
            auto_decompress=True,
        ) as session:
            for _hop in range(_ALLOWED_REDIRECT_HOPS + 1):
                # Token ONLY on the API origin, never on redirects.
                is_api = current == GITHUB_API_ORIGIN or current.startswith(
                    GITHUB_API_ORIGIN + "/"
                )
                headers = {"Accept": "application/vnd.github+json"}
                if token and is_api:
                    headers["Authorization"] = f"Bearer {token}"
                async with session.get(
                    current, headers=headers, allow_redirects=False
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location") or ""
                        assert_url_allowed(loc)
                        current = loc
                        continue
                    if resp.status == 403 and resp.headers.get(
                        "X-RateLimit-Remaining"
                    ) == "0":
                        raise UpdateCheckError("rate_limited")
                    if resp.status == 404:
                        raise UpdateCheckError(
                            "not_configured", "release source not found"
                        )
                    if resp.status != 200:
                        raise UpdateCheckError(
                            "http_error", f"HTTP {resp.status}"
                        )
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in resp.content.iter_any():
                        received += len(chunk)
                        if received > MAX_RESPONSE_BYTES:
                            raise UpdateCheckError("oversized")
                        chunks.append(chunk)
                    return b"".join(chunks)
            raise UpdateCheckError("network_error", "too many redirects")
    except UpdateCheckError:
        raise
    except asyncio.TimeoutError:
        raise UpdateCheckError("timeout") from None
    except Exception as e:  # noqa: BLE001
        raise UpdateCheckError("network_error", type(e).__name__) from None


# ----------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------
class UpdateService:
    def __init__(
        self,
        *,
        current_version: str,
        secret_store,
        db,
        data_dir: Path,
        http_json_fn: Callable = http_json,
        http_text_fn: Callable = http_text,
    ) -> None:
        self._current = current_version
        self._secrets = secret_store
        self._db = db
        self._data_dir = data_dir
        self._http_json = http_json_fn
        self._http_text = http_text_fn

    # ---- configuration ------------------------------------------------
    def releases_api_url(self) -> Optional[str]:
        """Operator-configured origin (live acceptance #6): env first,
        then the project config file; NO hardcoded repository."""
        url = os.environ.get("SHMAX_UPDATE_REPO_URL", "").strip()
        if not url:
            f = self._data_dir / REPO_FILE_SUBPATH
            try:
                url = f.read_text(encoding="utf-8").strip()
            except OSError:
                url = ""
        return parse_repo_url(url) if url else None

    def channel(self) -> str:
        if self._secrets.has_secret(CHANNEL_KEY):
            ch = self._secrets.get_secret(CHANNEL_KEY)
            return ch if ch in SUPPORTED_CHANNELS else "stable"
        return "stable"

    def set_channel(self, channel: str) -> str:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError("invalid channel")
        self._secrets.set_secret(CHANNEL_KEY, channel)
        return channel

    def github_token(self) -> str:
        # Optional, SecretStore-held, never logged.
        if self._secrets.has_secret("github_api_token"):
            return self._secrets.get_secret("github_api_token") or ""
        return ""

    # ---- persistence ---------------------------------------------------
    def state(self) -> dict:
        """Current persisted state (sync — reads SQLite directly)."""
        db_path = self._data_dir / "db" / "shmax.db"
        if not db_path.is_file():
            return {}
        try:
            con = sqlite3.connect(str(db_path))
            try:
                row = con.execute(
                    "SELECT value FROM settings WHERE key = ?", (STATE_KEY,)
                ).fetchone()
            finally:
                con.close()
            if row:
                return json.loads(row[0])
        except Exception:  # noqa: BLE001
            pass
        return {}

    async def _save_state(self, state: dict) -> None:
        """Persist check state (best-effort; never raises into the
        caller — a failed write must not break the bridge)."""
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

        from ..database.models import Setting

        try:
            async with self._db.session() as s:
                await s.execute(
                    _sqlite_insert(Setting)
                    .values(key=STATE_KEY, value=json.dumps(state))
                    .on_conflict_do_update(
                        index_elements=[Setting.key],
                        set_={"value": json.dumps(state)},
                    )
                )
                await s.commit()
        except Exception:  # noqa: BLE001
            logger.exception("could not persist update check state")

    # ---- discovery ------------------------------------------------------
    def _pick_release(self, releases: list[dict], channel: str) -> dict:
        best: Optional[dict] = None
        for rel in releases:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue
            if channel == "stable" and rel.get("prerelease"):
                continue
            tag = str(rel.get("tag_name") or "")
            if parse_version(tag) is None:
                continue
            if best is None or _cmp_versions(tag, str(best.get("tag_name"))) > 0:
                best = rel
        if best is None:
            raise UpdateCheckError("up_to_date")
        return best

    async def _artifact_meta(self, release: dict) -> ReleaseMeta:
        assets = release.get("assets") or []
        by_name = {
            str(a.get("name")): a for a in assets if isinstance(a, dict)
        }
        tag = str(release.get("tag_name") or "")
        artifact_name = f"shmax-{tag}.tar.gz"
        artifact = by_name.get(artifact_name)
        if artifact is None:
            # Fallback: the single shmax-*.tar.gz asset.
            tars = [n for n in by_name if n.startswith("shmax-") and n.endswith(".tar.gz")]
            if len(tars) == 1:
                artifact_name = tars[0]
                artifact = by_name[artifact_name]
        if artifact is None:
            raise UpdateCheckError("bad_manifest", "no release artifact")
        sums_asset = by_name.get("SHA256SUMS")
        manifest_asset = by_name.get("release-manifest.json")
        manifest_sha: Optional[str] = None
        sums_sha: Optional[str] = None
        if manifest_asset is not None:
            data = await self._http_json(
                str(manifest_asset.get("browser_download_url") or ""),
                self.github_token(),
            )
            if not isinstance(data, dict) or not data.get("sha256"):
                raise UpdateCheckError("bad_manifest", "manifest missing sha256")
            declared_version = str(data.get("version") or "")
            if declared_version and declared_version.lstrip("v") != tag.lstrip("v"):
                raise UpdateCheckError("bad_manifest", "manifest version mismatch")
            if str(data.get("artifact") or artifact_name) != artifact_name:
                raise UpdateCheckError("bad_manifest", "manifest artifact mismatch")
            manifest_sha = normalize_sha256(str(data["sha256"]))
        if sums_asset is not None:
            sums_raw = await self._http_text(
                str(sums_asset.get("browser_download_url") or ""),
                self.github_token(),
            )
            for line in sums_raw.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == artifact_name:
                    sums_sha = normalize_sha256(parts[0])
                    break
            if sums_sha is None:
                raise UpdateCheckError(
                    "bad_manifest", "artifact missing from SHA256SUMS"
                )
        # Live acceptance A5: when BOTH trust sources exist they
        # MUST agree; a disagreement fails the update.
        if manifest_sha is not None and sums_sha is not None:
            if manifest_sha != sums_sha:
                raise UpdateCheckError(
                    "bad_manifest", "manifest and SHA256SUMS disagree"
                )
        sha256 = manifest_sha or sums_sha
        if not sha256:
            raise UpdateCheckError("bad_manifest", "no trusted sha256")
        return ReleaseMeta(
            version=tag,
            channel="beta" if release.get("prerelease") else "stable",
            notes=str(release.get("body") or ""),
            published_at=str(release.get("published_at") or ""),
            artifact_url=str(artifact.get("browser_download_url") or ""),
            artifact_name=artifact_name,
            sha256=sha256,
        )

    async def check(self, *, persist: bool = True) -> dict:
        """One discovery round. Returns the full status dict."""
        now = int(_dt.datetime.now().timestamp())
        state: dict = {
            "last_checked_at": now,
            "current_version": self._current,
            "channel": self.channel(),
        }
        api_url = self.releases_api_url()
        if api_url is None:
            state["status"] = "not_configured"
        else:
            try:
                releases = await self._http_json(api_url, self.github_token())
                if not isinstance(releases, list):
                    raise UpdateCheckError("bad_manifest", "unexpected payload")
                picked = self._pick_release(releases, state["channel"])
                meta = await self._artifact_meta(picked)
                if _cmp_versions(meta.version, self._current) > 0:
                    state["status"] = "update_available"
                    state["available"] = meta.to_dict()
                    state["latest_known_version"] = meta.version
                else:
                    state["status"] = "up_to_date"
                    state["latest_known_version"] = meta.version
            except UpdateCheckError as e:
                if e.kind == "up_to_date":
                    state["status"] = "up_to_date"
                else:
                    state["status"] = e.kind
        if persist:
            await self._save_state(state)
        return self._status_dict(state)

    def _status_dict(self, state: dict) -> dict:
        return {
            "current_version": self._current,
            "channel": state.get("channel", self.channel()),
            "last_checked_at": state.get("last_checked_at", 0),
            "latest_known_version": state.get("latest_known_version"),
            "status": state.get("status", "not_checked"),
            "available": state.get("available"),
        }

    def status(self) -> dict:
        return self._status_dict(self.state())

    async def auto_check(self) -> None:
        """24h loop; every error swallowed — GitHub availability
        NEVER affects the bridge (live acceptance #15).

        The FIRST check is delayed (startup must not race the
        lifespan/DB bootstrap with a background write); the status
        endpoint reads the persisted state meanwhile."""
        import asyncio

        await asyncio.sleep(FIRST_AUTO_CHECK_DELAY_SECONDS)
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001
                logger.debug("auto update check failed", exc_info=True)
            await asyncio.sleep(AUTO_CHECK_INTERVAL_SECONDS)


__all__ = [
    "AUTO_CHECK_INTERVAL_SECONDS",
    "ReleaseMeta",
    "UpdateCheckError",
    "UpdateService",
    "http_json",
    "http_text",
    "is_prerelease",
    "parse_repo_url",
    "parse_version",
]
