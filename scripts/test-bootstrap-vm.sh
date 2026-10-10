#!/usr/bin/env bash
# Disposable Debian 13 amd64 VM. Never changes the runner's SSH or firewall.
set -Eeuo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
candidate=${1:?signed candidate directory required}
report=${2:?report directory required}
mkdir -p "$report"
tmp=$(mktemp -d)
cleanup() { if [[ -f "$tmp/qemu.pid" ]]; then sudo kill "$(sudo cat "$tmp/qemu.pid")" || true; fi; rm -rf "$tmp"; }
trap cleanup EXIT
url=https://cloud.debian.org/images/cloud/trixie/20261001-2618/debian-13-genericcloud-amd64-20261001-2618.qcow2
curl --proto '=https' --tlsv1.2 -fSL "$url" -o "$tmp/debian.qcow2"
printf '%s  %s\n' f46f0671a6e5bdec5291ab8972bae2f10e5408c2f64a74078f11efc2f06a436a9d0313ed50e0472542eeabf780e9f7c792ac0a314c6c20507fcd9fd81b468c3d "$tmp/debian.qcow2" | sha512sum -c -
qemu-img resize "$tmp/debian.qcow2" 30G
ssh-keygen -t ed25519 -N '' -f "$tmp/vm-key" -q
printf '#cloud-config\nssh_authorized_keys:\n  - %s\n' "$(cat "$tmp/vm-key.pub")" > "$tmp/user-data"
printf 'instance-id: shmax-bootstrap-test\nlocal-hostname: shmax-test\n' > "$tmp/meta-data"
cloud-localds "$tmp/seed.img" "$tmp/user-data" "$tmp/meta-data"
touch "$report/serial.log"
accel=tcg
if [[ -c /dev/kvm ]]; then accel=kvm; fi
printf 'VM accelerator: %s\n' "$accel" > "$report/vm.txt"
sudo qemu-system-x86_64 -machine q35,accel="$accel" -cpu max -smp 4 -m 4096 \
    -drive "file=$tmp/debian.qcow2,format=qcow2,if=virtio" \
    -drive "file=$tmp/seed.img,format=raw,if=virtio" \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=net0 \
    -display none -serial "file:$report/serial.log" -daemonize -pidfile "$tmp/qemu.pid"
ssh_args=(-i "$tmp/vm-key" -p 2222 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$tmp/known_hosts")
guest() { ssh "${ssh_args[@]}" debian@127.0.0.1 "$@"; }
for i in $(seq 1 120); do if guest true 2>/dev/null; then break; fi; sleep 5; done
printf 'Waiting for cloud-init and checking clean Debian host...\n'
guest 'sudo cloud-init status --wait; test "$(dpkg --print-architecture)" = amd64; . /etc/os-release; test "$ID:$VERSION_ID" = debian:13; ! command -v docker' > "$report/preflight.log" 2>&1
printf 'Clean Debian 13 amd64 verified; Docker is absent.\n'
# Transfer only public code and signed assets; no GitHub or signing credentials enter VM.
tar -C "$candidate" -cf - SHA256SUMS SHA256SUMS.sig release-manifest.json shmax-v0.1.0.tar.gz | guest 'mkdir -p /tmp/candidate; tar -xf - -C /tmp/candidate'
cat "$root/install.sh" | guest 'cat > /tmp/install.sh'
# Logs contain a temporary setup token; redact it before publishing test artifacts.
printf 'Installing signed SHMAX candidate and Docker in the clean VM...\n'
guest 'sudo bash /tmp/install.sh --release-dir /tmp/candidate > /tmp/install.log 2>&1' || { guest 'sudo cat /tmp/install.log' | sed -E 's/[?]t=[^[:space:]]+/\?t=[REDACTED]/g' > "$report/install.log"; exit 1; }
guest 'sudo cat /tmp/install.log' | sed -E 's/[?]t=[^[:space:]]+/\?t=[REDACTED]/g' > "$report/install.log"
printf 'Initial installation completed; checking services and creating persistent data fixtures...\n'
guest 'sudo docker compose -f /opt/shmax/compose.yaml ps; sudo systemctl is-active shmax-hostd; sudo test -e /opt/shmax/.shmax-installed; sudo shmax version' > "$report/installed.log"
# Exercise missing stable-release path; it must stop without modifying deployment.
guest 'sudo shmax update > /tmp/stable-check.log 2>&1; test $? -ne 0; grep -q "No published stable" /tmp/stable-check.log' > "$report/stable-gate.log"
cat "$root/scripts/test-bootstrap-guest.py" | guest 'cat > /tmp/test-guest.py'
guest 'sudo python3 /tmp/test-guest.py seed' > "$report/data-seed.log"
printf 'Running existing-installation safe update...\n'
guest 'sudo bash /tmp/install.sh --yes --release-dir /tmp/candidate > /tmp/update.log 2>&1' || { guest 'sudo cat /tmp/update.log' | sed -E 's/[?]t=[^[:space:]]+/\?t=[REDACTED]/g' > "$report/update.log"; exit 1; }
guest 'sudo cat /tmp/update.log' | sed -E 's/[?]t=[^[:space:]]+/\?t=[REDACTED]/g' > "$report/update.log"
guest 'sudo python3 /tmp/test-guest.py verify' > "$report/preserved-data.log"
printf 'Update completed; forcing a candidate rejection to verify cold-data rollback...\n'
cat "$root/scripts/test-bootstrap-rollback.py" | guest 'cat > /tmp/test-rollback.py'
guest 'sudo /opt/shmax-hostd/.venv/bin/python /tmp/test-rollback.py' > "$report/rollback.log" 2>&1
guest 'sudo python3 /tmp/test-guest.py verify' > "$report/rollback-preserved-data.log"
printf 'PASS: clean Debian 13 amd64 install, Docker/Compose/systemd/readiness, missing-stable fail-closed, repeated installer update, saved-data preservation and forced-failure cold-data/image rollback. Candidate supplied locally with production signature checks; release remains unpublished.\n' > "$report/result.txt"
