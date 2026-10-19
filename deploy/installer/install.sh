#!/usr/bin/env bash
# ============================================================
# SHMAX installer — Debian 13 V1
#
# Modes (auto-detected if not passed):
#   * install  — first-time install on a clean host.
#   * repair   — re-run the install steps without touching data.
#   * upgrade  — refresh deployment artifacts while keeping data.
#   * uninstall— remove the application; keep /var/lib/shmax by
#                default, or purge with --purge.
#
# Common flags:
#   * --dry-run    — print what would happen; never modify state.
#                    Works on macOS / CI without root.
#                    Required on macOS for any production-equivalent
#                    operation.
#   * --mode MODE  — override mode auto-detection.
#   * --purge      — uninstall only: also wipe /var/lib/shmax.
#   * --image IMG  — image to pull (default: shmax:${SHMAX_VERSION}).
#   * --no-start   — install mode: lay down files only, do not
#                    `docker compose up -d` or poll health.
#   * --no-ready   — print SHMAX READY only when /health/ready 200s.
#                    This is the default and the script aborts if
#                    readiness never returns within --ready-timeout.
#   * --ready-timeout SECONDS  — max wait for /health/ready
#                    (default 90s).
#
# Production gate: SHMAX refuses to perform real install / repair /
# upgrade / uninstall on macOS. --dry-run is the only legal macOS
# mode. This matches the V1 promise: production = Debian 13.
#
# UID/GID: default 8787. The installer refuses to override silently
# when the requested UID/GID is owned by another system principal.
# ============================================================

set -o errexit
set -o nounset
set -o pipefail

readonly SHMAX_OPT="/opt/shmax"
readonly SHMAX_DATA="/var/lib/shmax"
readonly SHMAX_PORT="${SHMAX_PORT:-8787}"
readonly SHMAX_VERSION="${SHMAX_VERSION:-0.1.2}"
readonly SHMAX_MARKER="${SHMAX_OPT}/.shmax-installed"
readonly SHMAX_USER="shmax"
readonly SHMAX_GROUP="shmax"
readonly SHMAX_UID_DEFAULT=8787
readonly SHMAX_GID_DEFAULT=8787
readonly SHMAX_IMAGE_DEFAULT="shmax:${SHMAX_VERSION}"
readonly SHMAX_HEALTH_PATH="/health"
readonly SHMAX_READY_PATH="/health/ready"

# --- Logging ------------------------------------------------------
log()  { printf '\033[1;34m[shmax]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[shmax]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[shmax]\033[0m %s\n' "$*" >&2; exit 1; }

# --- Argument parsing --------------------------------------------
DRY_RUN=0
PURGE=0
MODE_OVERRIDE=""
SHMAX_IMAGE="$SHMAX_IMAGE_DEFAULT"
NO_START=0
NO_READY=0
READY_TIMEOUT=90
SHMAX_UID="$SHMAX_UID_DEFAULT"
SHMAX_GID="$SHMAX_GID_DEFAULT"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)        DRY_RUN=1; shift ;;
        --purge)          PURGE=1; shift ;;
        --mode)           MODE_OVERRIDE="${2:-}"; shift 2 ;;
        --mode=*)         MODE_OVERRIDE="${1#*=}"; shift ;;
        --image)          SHMAX_IMAGE="${2:-}"; shift 2 ;;
        --image=*)        SHMAX_IMAGE="${1#*=}"; shift ;;
        --no-start)       NO_START=1; shift ;;
        --no-ready)       NO_READY=1; shift ;;
        --ready-timeout)  READY_TIMEOUT="${2:-90}"; shift 2 ;;
        --ready-timeout=*) READY_TIMEOUT="${1#*=}"; shift ;;
        --uid)            SHMAX_UID="${2:-}"; shift 2 ;;
        --uid=*)          SHMAX_UID="${1#*=}"; shift ;;
        --gid)            SHMAX_GID="${2:-}"; shift 2 ;;
        --gid=*)          SHMAX_GID="${1#*=}"; shift ;;
        --help|-h)
            sed -n '2,40p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *)
            warn "unknown flag: $1 (ignored)"
            shift ;;
    esac
done

if [[ $DRY_RUN -eq 1 ]]; then
    log "dry-run mode: no system changes"
fi

# --- Preflight helpers -------------------------------------------
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m(dry) %s\033[0m\n' "$*"
    else
        "$@"
    fi
}

run_quiet() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m(dry) %s\033[0m\n' "$*"
    else
        "$@" >/dev/null 2>&1
    fi
}

detect_arch() {
    local arch
    arch="$(uname -m 2>/dev/null || echo unknown)"
    case "$arch" in
        x86_64)        echo "amd64" ;;
        aarch64|arm64) echo "arm64" ;;
        armv7l|armv7)  echo "armv7 (experimental)" ;;
        *)             echo "$arch" ;;
    esac
}

detect_os() {
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        printf '%s %s\n' "${ID:-linux}" "${VERSION_ID:-?}"
    else
        echo "macos $(sw_vers -productVersion 2>/dev/null || echo '?')"
    fi
}

is_debian() {
    [[ -r /etc/os-release ]] || return 1
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "debian" ]] || return 1
    [[ "${VERSION_ID:-}" == "13"* ]] || return 1
    return 0
}

is_macos() {
    [[ "$(uname -s)" == "Darwin" ]]
}

have_docker() {
    command -v docker >/dev/null 2>&1
}

have_compose_v2() {
    docker compose version 2>/dev/null | grep -q "Docker Compose version" \
        && return 0
    return 1
}

# BLOCKER 20 — install Docker Engine + Compose v2 if missing.
# Idempotent: if Docker is already installed we leave it alone.
ensure_docker() {
    if have_docker && have_compose_v2; then
        log "docker engine + compose v2: present"
        return 0
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        log "(dry) would install docker engine + compose v2 via apt"
        return 0
    fi
    if is_macos; then
        fail "Docker is missing on macOS; install Docker Desktop before running the installer"
    fi
    log "installing docker engine + compose v2 via apt"
    run apt-get update
    run apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -e /etc/apt/keyrings/docker.asc ]]; then
        run curl -fsSL https://download.docker.com/linux/debian/gpg \
            -o /etc/apt/keyrings/docker.asc
        run chmod a+r /etc/apt/keyrings/docker.asc
    fi
    local arch
    arch="$(dpkg --print-architecture)"
    cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME:-bookworm} stable
EOF
    run apt-get update
    run apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    if ! have_docker; then
        fail "docker engine install did not produce a docker binary"
    fi
    if ! have_compose_v2; then
        fail "docker compose v2 plugin install did not produce a working compose"
    fi
    log "docker engine + compose v2: installed"
}

check_root() {
    if [[ $DRY_RUN -eq 1 ]]; then
        log "pretending we have root"
        return
    fi
    if [[ "$(uname -s)" == "Linux" ]] && [[ "$(id -u)" -ne 0 ]]; then
        fail "installer must run as root on Linux (use --dry-run on macOS / CI)"
    fi
}

check_production_target() {
    # Production install / repair / upgrade / uninstall is forbidden
    # on macOS. --dry-run is the only legal macOS mode.
    if is_macos && [[ $DRY_RUN -eq 0 ]]; then
        fail "production install on macOS is not supported (use --dry-run)"
    fi
}

# --- UID / GID handling -------------------------------------------
# Refuse silent reuse of an existing system UID/GID.
check_uid_collision() {
    local id="$1"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m(dry) check uid/gid %s\033[0m\n' "$id"
        return
    fi
    if getent passwd "$id" >/dev/null 2>&1; then
        local owner
        owner="$(getent passwd "$id" | cut -d: -f1)"
        if [[ "$owner" != "$SHMAX_USER" ]]; then
            fail "UID $id is owned by '$owner'; refuse to reuse"
        fi
    fi
    if getent group "$id" >/dev/null 2>&1; then
        local gowner
        gowner="$(getent group "$id" | cut -d: -f1)"
        if [[ "$gowner" != "$SHMAX_GROUP" ]]; then
            fail "GID $id is owned by '$gowner'; refuse to reuse"
        fi
    fi
}

is_installed() {
    [[ -e "${SHMAX_MARKER}" ]]
}

detect_mode() {
    if [[ -n "$MODE_OVERRIDE" ]]; then
        echo "$MODE_OVERRIDE"
        return
    fi
    if is_installed; then
        echo "repair"
    else
        echo "install"
    fi
}

detect_lan_ip() {
    local ip=""
    if command -v hostname >/dev/null 2>&1; then
        ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    fi
    if [[ -z "$ip" ]] && command -v ipconfig >/dev/null 2>&1; then
        ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
    fi
    printf '%s' "${ip:-127.0.0.1}"
}

# P0 #14 — appliance bind model. The compose default publishes on
# loopback only; the installer makes the appliance reachable from
# the owner's LAN by explicitly writing the detected (or
# SHMAX_BIND_ADDR-provided) address into the compose env file.
# Nothing is ever published on 0.0.0.0 by accident.
ensure_bind_addr() {
    local addr="${SHMAX_BIND_ADDR:-}"
    if [[ -z "$addr" && -f "${SHMAX_OPT}/.env" ]]; then
        addr="$(sed -n 's/^SHMAX_BIND_ADDR=//p' "${SHMAX_OPT}/.env" | tail -1)"
    fi
    if [[ -z "$addr" ]]; then
        addr="$(detect_lan_ip)"
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m(dry) write SHMAX_BIND_ADDR=%s to %s/.env\033[0m\n' \
            "$addr" "${SHMAX_OPT}"
    else
        mkdir -p "${SHMAX_OPT}"
        local env_file="${SHMAX_OPT}/.env"
        if [[ -f "$env_file" ]]; then
            grep -Ev '^SHMAX_(BIND_ADDR|PORT|UID|GID)=' "$env_file" > "${env_file}.tmp" || true
            mv "${env_file}.tmp" "$env_file"
        fi
        printf 'SHMAX_BIND_ADDR=%s\nSHMAX_PORT=%s\nSHMAX_UID=%s\nSHMAX_GID=%s\n' \
            "$addr" "$SHMAX_PORT" "$SHMAX_UID" "$SHMAX_GID" >> "$env_file"
        log "web UI bound to: ${addr} (SHMAX_BIND_ADDR)"
    fi
    BIND_ADDR="$addr"
}

# Canonical host used by installer-side probes (readiness, token
# mint). Rules (surgical):
#   * concrete LAN bind (e.g. 192.0.2.161) -> probe that address —
#     the container port is published there and 127.0.0.1 is NOT;
#   * 127.0.0.1 bind -> probe 127.0.0.1;
#   * 0.0.0.0 bind -> probe 127.0.0.1.
# Normal LAN bind is never widened to 0.0.0.0 for probes.
shmax_probe_host() {
    # PROBE address (installer-side checks). BIND_ADDR is set by
    # ensure_bind_addr() during install/repair; SHMAX_BIND_ADDR
    # covers direct env invocation. Wildcard binds probe loopback.
    local addr="${BIND_ADDR:-${SHMAX_BIND_ADDR:-}}"
    case "$addr" in
        ""|"127.0.0.1"|"::1"|"0.0.0.0")
            printf '%s' "127.0.0.1"
            ;;
        *)
            printf '%s' "$addr"
            ;;
    esac
}

# OWNER/DISPLAY address (what the operator types in a browser).
# Wildcard binds are reachable on the LAN — display the detected
# usable LAN IP, never 0.0.0.0.
shmax_owner_host() {
    local addr="${BIND_ADDR:-${SHMAX_BIND_ADDR:-}}"
    case "$addr" in
        ""|"127.0.0.1"|"::1")
            printf '%s' "127.0.0.1"
            ;;
        "0.0.0.0")
            detect_lan_ip
            ;;
        *)
            printf '%s' "$addr"
            ;;
    esac
}

shmax_base_url() {
    printf 'http://%s:%s' "$(shmax_probe_host)" "${SHMAX_PORT}"
}

show_bind_hint() {
    cat <<'HINT'

  Bind address: SHMAX publishes on the LAN address written to
  /opt/shmax/.env (SHMAX_BIND_ADDR). Override it explicitly with
    SHMAX_BIND_ADDR=192.168.1.50 ./install.sh
  or restrict the appliance to this machine only with
    SHMAX_BIND_ADDR=127.0.0.1 ./install.sh
HINT
}

# --- Persistent layout -------------------------------------------
ensure_user() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m(dry) id %s\033[0m\n' "$SHMAX_USER"
        return
    fi
    if ! id "$SHMAX_USER" >/dev/null 2>&1; then
        log "creating system user ${SHMAX_USER} (uid=${SHMAX_UID} gid=${SHMAX_GID})"
        # Pin the UID/GID explicitly. The container image bakes
        # the same UID/GID into its runtime user, so the host
        # ownership of /var/lib/shmax must match exactly — a
        # generic ``useradd`` would pick the next free system
        # uid and break the bind-mount permissions.
        if ! getent group "$SHMAX_GROUP" >/dev/null; then
            groupadd --system --gid "$SHMAX_GID" "$SHMAX_GROUP"
        fi
        useradd --system --home "${SHMAX_DATA}" --shell /usr/sbin/nologin \
                --gid "$SHMAX_GID" --uid "$SHMAX_UID" "$SHMAX_USER"
    else
        # Idempotent re-run on an existing host: align the
        # existing ``shmax`` uid/gid to the requested value so
        # a previous misconfigured install can be repaired.
        current_uid=$(id -u "${SHMAX_USER}")
        if [[ "${current_uid}" != "${SHMAX_UID}" ]]; then
            log "re-aligning ${SHMAX_USER} uid ${current_uid} -> ${SHMAX_UID}"
            usermod -u "${SHMAX_UID}" "${SHMAX_USER}"
        fi
        current_gid=$(id -g "${SHMAX_USER}")
        if [[ "${current_gid}" != "${SHMAX_GID}" ]]; then
            log "re-aligning ${SHMAX_USER} gid ${current_gid} -> ${SHMAX_GID}"
            groupmod -g "${SHMAX_GID}" "${SHMAX_GROUP}"
        fi
    fi
}

ensure_persistent_root() {
    log "creating persistent layout under ${SHMAX_DATA}"
    for sub in db config secrets sessions/max sessions/telegram state backups cache; do
        run mkdir -p "${SHMAX_DATA}/${sub}"
    done
    run chmod 0700 "${SHMAX_DATA}"
    run chmod 0700 "${SHMAX_DATA}/secrets"
    run chmod 0750 "${SHMAX_DATA}/db" "${SHMAX_DATA}/state" "${SHMAX_DATA}/backups"
    if [[ $DRY_RUN -eq 0 ]]; then
        chown -R "${SHMAX_USER}:${SHMAX_GROUP}" "${SHMAX_DATA}" 2>/dev/null || true
    fi
}

ensure_setup_secret() {
    # The backend's /api/setup/token endpoint accepts calls from
    # either the loopback interface OR a caller that presents
    # the ``X-Shmax-Setup-Secret`` header. The installer is
    # outside the container, so it uses the header path. The
    # secret is generated on the host, written to the
    # persistent ``state/`` directory, and pinned to the same
    # value across re-installs so the installer can be re-run
    # idempotently.
    if [[ $DRY_RUN -eq 1 ]]; then
        return 0
    fi
    local secret_path="${SHMAX_DATA}/state/setup_secret"
    mkdir -p "${SHMAX_DATA}/state"
    if [[ ! -f "${secret_path}" ]]; then
        log "generating setup secret"
        head -c 32 /dev/urandom | base64 > "${secret_path}"
        chmod 0600 "${secret_path}"
        chown "${SHMAX_USER}:${SHMAX_GROUP}" "${secret_path}"
    fi
}

ensure_opt_layout() {
    log "populating ${SHMAX_OPT}"
    run mkdir -p "${SHMAX_OPT}"
    run cp -f "$(dirname "${BASH_SOURCE[0]}")/install.sh" "${SHMAX_OPT}/install.sh"
    if [[ -d "$(dirname "${BASH_SOURCE[0]}")/../compose" ]]; then
        run cp -f "$(dirname "${BASH_SOURCE[0]}")/../compose/compose.yaml" "${SHMAX_OPT}/compose.yaml"
    fi
    # Live acceptance A11: the boot orchestration script ships with
    # the deployment (systemd unit ExecStart points here).
    if [[ -f "$(dirname "${BASH_SOURCE[0]}")/../boot/shmax-boot.sh" ]]; then
        run mkdir -p "${SHMAX_OPT}/boot"
        run cp -f "$(dirname "${BASH_SOURCE[0]}")/../boot/shmax-boot.sh" "${SHMAX_OPT}/boot/shmax-boot.sh"
        run chmod 0755 "${SHMAX_OPT}/boot/shmax-boot.sh"
    fi
}

# Live acceptance A11: install/refresh the systemd boot unit
# idempotently. The unit waits for the configured bind address,
# recreates the container when the host-port publication is
# broken and verifies HOST-visible readiness.
ensure_boot_unit() {
    local unit_src="$(dirname "${BASH_SOURCE[0]}")/../systemd/shmax-boot.service"
    [[ -f "$unit_src" ]] || fail "missing shmax-boot.service"
    run install -m 0644 "$unit_src" /etc/systemd/system/shmax-boot.service
}

ensure_hostd_runtime() {
    local source_root
    source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    if [[ $DRY_RUN -eq 1 ]]; then
        log "(dry) install python3-venv and host runtime at /opt/shmax-hostd"
        return 0
    fi
    apt-get update
    apt-get install -y --no-install-recommends python3 python3-venv
    # Stop before replacing imported runtime files. A failed installation stays failed.
    if systemctl cat shmax-hostd.service >/dev/null 2>&1; then
        systemctl stop shmax-hostd.service
    fi
    bash "${source_root}/deploy/hostd/install-runtime.sh" "$source_root" /opt/shmax-hostd
    ln -sfn /opt/shmax-hostd/bin/shmax /usr/local/bin/shmax
}

ensure_host_units() {
    ensure_boot_unit
    local hostd_src="$(dirname "${BASH_SOURCE[0]}")/../systemd/shmax-hostd.service"
    [[ -f "$hostd_src" ]] || fail "missing shmax-hostd.service"
    run install -m 0644 "$hostd_src" /etc/systemd/system/shmax-hostd.service
    if [[ $DRY_RUN -eq 1 ]]; then
        log "(dry) systemd-analyze verify boot/hostd; enable boot and enable --now hostd; verify socket"
        return 0
    fi
    [[ -x /opt/shmax/boot/shmax-boot.sh ]] || fail "boot executable missing"
    [[ -x /opt/shmax-hostd/.venv/bin/python ]] || fail "hostd executable missing"
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify /etc/systemd/system/shmax-boot.service \
            /etc/systemd/system/shmax-hostd.service
    else
        fail "systemd-analyze required on supported Debian production host"
    fi
    systemctl daemon-reload
    systemctl enable shmax-boot.service
    systemctl enable --now shmax-hostd.service
    local deadline=$((SECONDS + 30))
    until [[ -S /run/shmax/hostd.sock ]]; do
        systemctl is-active --quiet shmax-hostd.service || fail "hostd is not active"
        (( SECONDS < deadline )) || fail "hostd socket did not appear"
        sleep 1
    done
    [[ "$(stat -c '%u:%g:%a' /run/shmax/hostd.sock)" == "0:${SHMAX_GID}:660" ]] \
        || fail "hostd socket ownership/mode invalid"
    [[ "$(stat -c '%u:%g:%a' /run/shmax)" == "0:${SHMAX_GID}:750" ]] \
        || fail "hostd runtime directory ownership/mode invalid"
    # A connect under the container's numeric identity proves directory/socket access.
    setpriv --reuid="$SHMAX_UID" --regid="$SHMAX_GID" --clear-groups \
        /opt/shmax-hostd/.venv/bin/python -c \
        'import socket; s=socket.socket(socket.AF_UNIX); s.connect("/run/shmax/hostd.sock"); s.close()'
    log "boot and hostd units validated; hostd running with accessible 0660 socket"
}

# --- Pull / start / wait -----------------------------------------
wait_for_ready() {
    local deadline=$((SECONDS + READY_TIMEOUT))
    local url="$(shmax_base_url)${SHMAX_READY_PATH}"
    log "waiting for ${url} (timeout ${READY_TIMEOUT}s)"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m(dry) curl %s\033[0m\n' "$url"
        return 0
    fi
    while (( SECONDS < deadline )); do
        if curl -fsS -m 3 -o /dev/null "$url"; then
            log "service is healthy"
            return 0
        fi
        sleep 2
    done
    fail "service did not become ready within ${READY_TIMEOUT}s"
}

start_container() {
    if [[ $NO_START -eq 1 ]]; then
        log "--no-start: skipping docker compose up"
        return 0
    fi
    log "starting SHMAX container (image: ${SHMAX_IMAGE})"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m(dry) docker compose -f %s build\033[0m\n' \
            "${SHMAX_OPT}/compose.yaml"
        printf '  \033[2m(dry) docker compose -f %s up -d\033[0m\n' \
            "${SHMAX_OPT}/compose.yaml"
        return 0
    fi
    # Build from the source tree so the build context sees the
    # ``backend/``, ``frontend/`` and ``Dockerfile`` siblings.
    # The production compose lives at ``${SHMAX_OPT}/compose.yaml``
    # and uses a pre-built ``image:`` reference, so we build
    # first via the root ``compose.yaml`` (which carries
    # ``build:``) then ``up`` the production manifest.
    local source_root
    source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    source_root="$(cd "${source_root}/.." && pwd)"
    ensure_bind_addr
    log "building image from ${source_root}"
    ( cd "${source_root}" && \
      env SHMAX_DATA_DIR="${SHMAX_DATA}" \
          SHMAX_BIND_ADDR="${BIND_ADDR}" \
          SHMAX_PORT="${SHMAX_PORT}" \
          SHMAX_UID="${SHMAX_UID}" \
          SHMAX_GID="${SHMAX_GID}" \
          docker compose -f compose.yaml build \
        && cd "${SHMAX_OPT}" && \
           env SHMAX_DATA_DIR="${SHMAX_DATA}" \
               SHMAX_BIND_ADDR="${BIND_ADDR}" \
               SHMAX_PORT="${SHMAX_PORT}" \
               SHMAX_UID="${SHMAX_UID}" \
               SHMAX_GID="${SHMAX_GID}" \
               docker compose -f compose.yaml up -d )
    wait_for_ready
}

mint_setup_token() {
    # BLOCKER 20 — the setup token must come from the LIVE
    # backend, not from a ``date | sha256sum`` synthetic. The
    # installer is responsible for calling ``/api/setup/token``
    # after the container is up. If we cannot reach the backend
    # we fail loudly instead of handing the operator a bogus
    # token.
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '%s' "dryrun-setup-token"
        return 0
    fi
    # The /api/setup/token endpoint accepts the request from
    # either a localhost caller OR a caller that presents the
    # ``X-Shmax-Setup-Secret`` header. The installer is
    # explicitly NOT inside the container, so we use the
    # secret-header path. The secret was written by
    # ``ensure_setup_secret`` to ``/var/lib/shmax/state/setup_secret``
    # before the container started.
    local url="$(shmax_base_url)/api/setup/token"
    local secret=""
    local secret_path="${SHMAX_DATA}/state/setup_secret"
    if [[ -f "${secret_path}" ]]; then
        secret="$(cat "${secret_path}")"
    fi
    local payload
    if [[ -n "${secret}" ]]; then
        payload="$(curl -fsS --max-time 10 -X POST \
            -H "X-Shmax-Setup-Secret: ${secret}" \
            "${url}" 2>/dev/null || true)"
    else
        payload="$(curl -fsS --max-time 10 -X POST "${url}" 2>/dev/null || true)"
    fi
    if [[ -n "${payload}" ]] && command -v jq >/dev/null 2>&1; then
        local token
        token="$(printf '%s' "${payload}" | jq -r '.token // empty')"
        if [[ -n "${token}" ]]; then
            printf '%s' "${token}"
            return 0
        fi
    fi
    if [[ -n "${payload}" ]]; then
        # No jq; try python which we know is on every Debian
        # install because the runtime needs it.
        local token
        token="$(printf '%s' "${payload}" | python3 -c 'import json,sys;d=json.loads(sys.stdin.read() or "{}");print(d.get("token",""))' 2>/dev/null || true)"
        if [[ -n "${token}" ]]; then
            printf '%s' "${token}"
            return 0
        fi
    fi
    # Live acceptance A10: a CONFIGURED appliance (admin exists,
    # upgrade/repair) must NOT mint a setup token — the backend
    # refuses and the installer must NOT fail. Return non-zero and
    # let the caller decide per mode; exit only for a genuine
    # first install where the token is REQUIRED.
    return 1
}

maybe_mint_setup_token() {
    # Live acceptance A10: setup-token generation happens ONLY for
    # a genuine first-time bootstrap. upgrade/repair NEVER require
    # one; install prints it only while setup is incomplete.
    if [[ "${1:-}" != "install" ]]; then
        printf '%s' ""
        return 0
    fi
    local token=""
    token="$(mint_setup_token)" && { printf '%s' "${token}"; return 0; }
    # First install WITHOUT a working token: this IS fatal — the
    # operator cannot bootstrap without it.
    fail "could not mint setup token from backend; refusing to fabricate one"
}

# --- Uninstall ----------------------------------------------------
do_uninstall() {
    log "uninstall mode"
    if [[ $DRY_RUN -eq 0 ]]; then
        ( cd "${SHMAX_OPT}" && docker compose -f compose.yaml down --remove-orphans 2>/dev/null || true )
    fi
    if [[ $PURGE -eq 1 ]]; then
        if [[ $DRY_RUN -eq 1 ]]; then
            warn "dry-run + --purge: would remove ${SHMAX_DATA}"
        else
            read -r -p "Type SHMAX to confirm PURGE of ${SHMAX_DATA}: " confirm
            if [[ "$confirm" != "SHMAX" ]]; then
                fail "purge aborted (confirmation did not match)"
            fi
        fi
        run rm -rf "${SHMAX_DATA}"
    else
        log "keeping ${SHMAX_DATA} (use --purge to remove)"
    fi
    if [[ $DRY_RUN -eq 0 ]]; then
        for unit in shmax-boot.service shmax-hostd.service; do
            if systemctl cat "$unit" >/dev/null 2>&1; then
                systemctl disable --now "$unit"
            fi
        done
    fi
    run rm -f /etc/systemd/system/shmax-boot.service /etc/systemd/system/shmax-hostd.service
    if [[ $DRY_RUN -eq 0 ]]; then
        systemctl daemon-reload
        if [[ "$(readlink /usr/local/bin/shmax 2>/dev/null || true)" == /opt/shmax-hostd/bin/shmax ]]; then
            rm -f /usr/local/bin/shmax
        fi
    fi
    run rm -rf /opt/shmax-hostd /run/shmax
    run rm -f "${SHMAX_MARKER}"
    run rm -rf "${SHMAX_OPT}"
    if [[ $DRY_RUN -eq 0 ]]; then
        if id "$SHMAX_USER" >/dev/null 2>&1; then
            userdel "$SHMAX_USER" 2>/dev/null || true
        fi
    fi
    log "uninstall complete"
}

# --- Repair / upgrade --------------------------------------------
do_repair() {
    log "repair mode: re-applying layout, preserving data"
    ensure_user
    check_uid_collision "$SHMAX_UID"
    ensure_persistent_root
    ensure_setup_secret
    ensure_opt_layout
    ensure_hostd_runtime
    ensure_host_units
    start_container
    write_marker
    log "repair complete"
}

do_upgrade() {
    log "upgrade mode: refreshing deployment artifacts"
    check_uid_collision "$SHMAX_UID"
    ensure_user
    ensure_persistent_root
    ensure_setup_secret
    ensure_opt_layout
    ensure_hostd_runtime
    ensure_host_units
    start_container
    log "upgrade complete"
}

# --- Install ------------------------------------------------------
do_install() {
    log "install mode"
    check_production_target
    if ! is_debian && [[ $DRY_RUN -eq 0 ]]; then
        fail "production install requires Debian 13"
    fi
    check_uid_collision "$SHMAX_UID"
    ensure_user
    ensure_persistent_root
    ensure_setup_secret
    ensure_opt_layout
    # BLOCKER 20 — install Docker Engine + Compose v2 if they
    # are missing. We do this BEFORE starting the container so
    # ``docker compose up -d`` can succeed.
    ensure_docker
    ensure_hostd_runtime
    ensure_host_units
    start_container
    write_marker
    log "install complete"
}

write_marker() {
    if [[ $DRY_RUN -eq 0 ]]; then
        mkdir -p "${SHMAX_OPT}"
        printf 'version=%s\ninstalled_at=%s\nmode=install\n' \
            "${SHMAX_VERSION}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            > "${SHMAX_MARKER}"
    fi
}

# --- Banner + QR --------------------------------------------------
print_setup_url() {
    local lan_ip="$1"
    local port="$2"
    local setup_token="${3:-}"
    local url
    if [[ -n "$setup_token" ]]; then
        url="http://${lan_ip}:${port}/setup?t=${setup_token}"
    else
        url="http://${lan_ip}:${port}/setup"
    fi
    printf '%s' "$url"
}

print_ready_banner() {
    local url="$1"
    local prefix="$2"
    cat <<EOF

${prefix}
╭──────────────────────────────────────────────╮
│                                              │
│              🦆 SHMAX READY                  │
│                                              │
│              Шмакс жив.                      │
│                                              │
╰──────────────────────────────────────────────╯

Откройте:

${url}

или отсканируйте QR:

EOF
}

print_qr() {
    local payload="$1"
    if command -v qrencode >/dev/null 2>&1; then
        qrencode -t UTF8 "$payload" || true
        return
    fi
    local py=""
    if [[ -n "${SHMAX_PY:-}" ]]; then
        py="$SHMAX_PY"
    elif [[ -x "$(dirname "${BASH_SOURCE[0]}")/../../backend/.venv/bin/python" ]]; then
        py="$(dirname "${BASH_SOURCE[0]}")/../../backend/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        py="python3"
    fi
    if [[ -n "$py" ]] && "$py" -c "import qrcode" 2>/dev/null; then
        "$py" -c "
import qrcode, sys
qr = qrcode.QRCode(border=1)
qr.add_data(sys.argv[1])
qr.make()
qr.print_ascii(out=sys.stdout)
" "$payload" || true
        return
    fi
    printf '(no QR tool available — open the URL above manually)\n'
}

# --- Main ---------------------------------------------------------
main() {
    local mode
    mode="$(detect_mode)"

    log "shell:           $(bash --version | head -1 | awk '{print $4}')"
    log "architecture:    $(detect_arch)"
    log "os:              $(detect_os)"
    log "version:         ${SHMAX_VERSION}"
    log "mode:            ${mode}"

    check_root

    if have_docker; then
        log "docker:          $(docker --version)"
        if have_compose_v2; then
            log "compose:         v2"
        else
            warn "Docker Compose v2 not detected; SHMAX requires v2"
        fi
    else
        warn "docker:          not found (install Docker Engine + Compose v2)"
    fi

    local lan_ip
    lan_ip="$(detect_lan_ip)"
    log "LAN IP:          ${lan_ip}"

    # Production-target gate: macOS refuses real install.
    check_production_target

    case "$mode" in
        install)   do_install ;;
        repair)    do_repair ;;
        upgrade)   do_upgrade ;;
        uninstall) do_uninstall; exit 0 ;;
        *)         fail "unknown mode: $mode" ;;
    esac

    local setup_token
    setup_token="$(maybe_mint_setup_token "$mode")"

    # The owner-facing host: concrete LAN bind -> that address;
    # wildcard bind -> detected usable LAN IP; loopback -> loopback.
    local ui_host
    ui_host="$(shmax_owner_host)"
    log "Web UI:          http://${ui_host}:${SHMAX_PORT}"

    local url
    url="$(print_setup_url "$ui_host" "$SHMAX_PORT" "$setup_token")"
    local banner_prefix
    if [[ $DRY_RUN -eq 1 ]]; then
        banner_prefix="DRY RUN — simulated READY"
    else
        banner_prefix="Health-check passed:"
    fi
    print_ready_banner "$url" "$banner_prefix"
    print_qr "$url"

    if [[ $DRY_RUN -eq 1 ]]; then
        log "dry-run complete; nothing was installed"
    else
        log "installation finished."
        log "web UI bind:      ${BIND_ADDR:-<SHMAX_BIND_ADDR>}:${SHMAX_PORT}"
        log "next: open ${url} to complete setup wizard."
        show_bind_hint
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi