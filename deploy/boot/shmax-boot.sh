#!/usr/bin/env bash
# SHMAX boot orchestration (live acceptance A11).
#
# Reproduced on real Debian 13: Docker restored the container before
# the configured LAN address existed -> the host port bind FAILED
# ("cannot assign requested address") -> container Exited(128); and
# after the address appeared, a plain `docker start` produced a
# running/"healthy" container WITHOUT a functional published port
# (NetworkSettings.Ports == {}).
#
# This script is the ONE boot supervisor for SHMAX:
#   1. wait (bounded, with backoff) for the configured bind address
#      to actually exist on the host;
#   2. ensure the container is (re)created with the requested host
#      binding — `docker compose up -d --force-recreate` when the
#      publication is missing or broken;
#   3. VERIFY the published host port (`docker port`), because the
#      container-internal healthcheck is NOT sufficient;
#   4. host-side readiness via curl to the configured bind URL;
#   5. exit non-zero on failure so systemd retries with backoff.
#
# No destructive data operations; /var/lib/shmax is never touched.
# Public Internet is NOT required.

set -u

SHMAX_OPT="${SHMAX_OPT:-/opt/shmax}"
SHMAX_DATA="${SHMAX_DATA:-/var/lib/shmax}"
SHMAX_BIND="${SHMAX_BIND:-${SHMAX_BIND_ADDR:-}}"
SHMAX_PORT="${SHMAX_PORT:-}"
# Bounded overall wait for the bind address (seconds).
WAIT_TOTAL="${SHMAX_BOOT_WAIT_TOTAL:-600}"
POLL_INTERVAL="${SHMAX_BOOT_POLL_INTERVAL:-3}"
CURL_TIMEOUT="${SHMAX_BOOT_CURL_TIMEOUT:-5}"

log() { printf '[shmax-boot] %s\n' "$*" >&2; }
fail() { log "FATAL: $*"; exit 1; }

# Read only the two known .env values; never execute operator configuration.
load_binding() {
    if [[ -f "${SHMAX_OPT}/.env" ]]; then
        local key value
        while IFS='=' read -r key value; do
            value="${value%\"}"; value="${value#\"}"
            value="${value%\'}"; value="${value#\'}"
            case "$key" in
                SHMAX_BIND_ADDR) [[ -n "$SHMAX_BIND" ]] || SHMAX_BIND="$value" ;;
                SHMAX_PORT) [[ -n "$SHMAX_PORT" ]] || SHMAX_PORT="$value" ;;
            esac
        done < "${SHMAX_OPT}/.env"
    fi
    SHMAX_PORT="${SHMAX_PORT:-8787}"
    [[ "$SHMAX_PORT" =~ ^[0-9]+$ ]] && (( SHMAX_PORT >= 1 && SHMAX_PORT <= 65535 )) \
        || fail "invalid configured port"
    [[ -z "$SHMAX_BIND" || "$SHMAX_BIND" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || fail "invalid configured IPv4 bind address"
}

# bind address from the compose file (first <ip>:<port>:<port>).
detect_bind() {
    if [[ -n "$SHMAX_BIND" ]]; then
        printf '%s' "$SHMAX_BIND"
        return 0
    fi
    local compose="${SHMAX_OPT}/compose.yaml"
    [[ -f "$compose" ]] || fail "no compose file at $compose"
    local ip
    ip="$(sed -n 's/.*"\([0-9.]*\):[0-9]*:[0-9]*".*/\1/p' "$compose" | head -1)"
    if [[ -n "$ip" && "$ip" != "0.0.0.0" ]]; then
        printf '%s' "$ip"
    else
        printf '127.0.0.1'
    fi
}

# address_exists <ip> — True when the IP is configured on a local
# interface (Linux: /proc/net/if_inet6 or `ip addr`; BSD fallback
# ifconfig). No Internet required.
address_exists() {
    local ip="$1"
    if command -v ip >/dev/null 2>&1; then
        ip -o addr show 2>/dev/null | grep -qw "$ip" && return 0
    fi
    if command -v ifconfig >/dev/null 2>&1; then
        ifconfig -a 2>/dev/null | grep -qw "inet $ip" && return 0
    fi
    return 1
}

# published_port_ok — the container actually publishes the host
# port on the bind address. INTERNAL health is NOT sufficient
# (live A11.1: running+healthy with EMPTY NetworkSettings.Ports).
published_port_ok() {
    local ip="$1" port="$2"
    docker port shmax-web-1 2>/dev/null | grep -F -- "-> ${ip}:${port}" | grep -q ":${port}$"
}

compose_up_recreate() {
    docker compose --project-directory "${SHMAX_OPT}" -f "${SHMAX_OPT}/compose.yaml" up -d --force-recreate
}

host_ready() {
    local ip="$1" port="$2"
    curl -fsS --max-time "$CURL_TIMEOUT" \
        "http://${ip}:${port}/health/ready" >/dev/null 2>&1
}

main() {
    command -v docker >/dev/null 2>&1 || fail "docker not found"
    [[ -f "${SHMAX_OPT}/compose.yaml" ]] || fail "no ${SHMAX_OPT}/compose.yaml"

    load_binding
    local bind probe_bind
    bind="$(detect_bind)"
    probe_bind="$bind"
    [[ "$bind" != "0.0.0.0" ]] || probe_bind="127.0.0.1"
    log "waiting for bind address ${bind} (up to ${WAIT_TOTAL}s)"

    # 1. bounded wait for the LOCAL address; no Internet needed.
    local waited=0
    while ! address_exists "$probe_bind"; do
        sleep "$POLL_INTERVAL"
        waited=$((waited + POLL_INTERVAL))
        if (( waited >= WAIT_TOTAL )); then
            fail "bind address ${bind} did not appear within ${WAIT_TOTAL}s"
        fi
    done
    log "address ${bind} is up"

    # 2/3. ensure the container runs AND publishes the host port.
    if ! docker ps --format '{{.Names}}' | grep -q '^shmax-web-1$' \
       || ! published_port_ok "$bind" "$SHMAX_PORT"; then
        log "(re)creating container to (re)establish host port binding"
        compose_up_recreate || fail "docker compose up failed"
    fi

    # Publication may STILL be broken after a plain up (the
    # A11.1 state) — force one recreate cycle and re-verify.
    if ! published_port_ok "$bind" "$SHMAX_PORT"; then
        log "port publication still missing — forcing recreate"
        compose_up_recreate || fail "docker compose up failed"
        published_port_ok "$bind" "$SHMAX_PORT" \
            || fail "published port ${bind}:${SHMAX_PORT} still missing"
    fi
    log "host port published: ${bind}:${SHMAX_PORT}"

    # 4. host-side readiness (bounded).
    local tries=0
    until host_ready "$probe_bind" "$SHMAX_PORT"; do
        tries=$((tries + 1))
        if (( tries * POLL_INTERVAL >= WAIT_TOTAL )); then
            fail "host readiness check did not pass at http://${bind}:${SHMAX_PORT}/health/ready"
        fi
        sleep "$POLL_INTERVAL"
    done
    log "SHMAX is ready at http://${bind}:${SHMAX_PORT}"
}

main "$@"
