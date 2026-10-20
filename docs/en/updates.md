# Updates (GitHub Releases)

Source: `SHMAX_UPDATE_REPO_URL=https://github.com/<owner>/<repo>`
(strict HTTPS GitHub; otherwise "not configured"). Channels:
stable (no prereleases) and beta; proper SemVer precedence.
Auto-check every 24h + manual check; GitHub being down never
affects the bridge. Trust: release-manifest.json and/or
SHA256SUMS (must agree), 64-hex sha256; allowlisted HTTPS GitHub
origins only, every redirect re-validated, the optional token goes
ONLY to api.github.com. Install is manual only and requires
shmax-hostd; the transaction (host-side `shmax update apply`):
download → verify SHA of downloaded bytes → safety backup →
staging → install to /opt/shmax (compose.yaml/.env preserved) →
recreate → HOST-visible readiness → success or automatic rollback
to the retained previous deployment. /var/lib/shmax is never
replaced by a release.

## Standard v0.1.3 command

`sudo shmax update` downloads the latest stable package and verifies its signature against the existing embedded key. For a verified local production archive use `sudo shmax update /path/to/shmax-v0.1.3.tar.gz`. UI/hostd still require an explicit administrator action.

The image builds before downtime. The updater then takes a cold persistent-data snapshot, verifies contents/ownership/permissions and SQLite integrity, starts the candidate and checks fresh configured MAX/Telegram readiness. Failure restores the exact previous image/deployment and verified cold data, retaining protected diagnostics. Release contents never replace the data directory; rollback restores its own cold snapshot. Keep complete backups away from a failing disk; configuration-only copies are insufficient. Historical failed deliveries are not replayed automatically.

[v0.1.3 changes](../../release/v0.1.3/RELEASE_NOTES.en.md) · [Event journal](event-journal.md).
