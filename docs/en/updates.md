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
