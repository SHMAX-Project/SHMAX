PY
archive=$(cat "$stage/archive-path")
version=$(cat "$stage/version")
step "Release signature and checksums verified: $version"
if (( existing )); then
    # Installed trusted transaction runner makes the cold backup and rollback.
    /usr/local/bin/shmax update "$archive"
    step 'Update verified; existing data and authorizations preserved'
else
    source=$(cat "$stage/source-path")
    rollback_install() {
        local status=$?
        trap - ERR
        printf '[!] Installation failed. Persistent data is retained; stopping incomplete SHMAX services.\n' >&2
        if [[ -f /opt/shmax/compose.yaml ]]; then
            docker compose --project-directory /opt/shmax -f /opt/shmax/compose.yaml stop web >/dev/null 2>&1 || true
        fi
        for unit in shmax-boot.service shmax-hostd.service; do
            systemctl disable --now "$unit" >/dev/null 2>&1 || true
        done
        printf '[!] Docker and downloaded system dependencies are retained. Repair the incomplete installation before retrying.\n' >&2
        exit "$status"
    }
    trap rollback_install ERR
    SHMAX_VERSION="${version#v}" bash "$source/deploy/installer/install.sh" --mode install
    trap - ERR
    step 'INSTALLATION COMPLETE — application health checked'
fi
printf '\nOpen the web interface printed above to complete setup.\n'
