# SHMAX v0.1.0 publication runbook — NOT EXECUTED

Execute only after “А ТЕПЕРЬ ВЫКЛАДЫВАЕМ”. All operations below are future actions,
not a publication script. Work from the independent **SHMAX-PUBLIC** repository;
never push the private SHMAX development archive.

1. Resolve organization/admin/public identity variables in
   [PUBLICATION_CONFIG.md](PUBLICATION_CONFIG.md). Authenticate with `gh auth status`
   without printing tokens. Verify the owner's organization membership/admin rights;
   create/select the organization in GitHub UI only after explicit authorization.
   Requested SHMAX login availability is not assumed.
2. Decide whether to retain the documented safe staging identity or approve its
   replacement in the separate candidate. If replacing commit identity, do so only
   in that public candidate with explicit approval; rescan all new history and
   rebuild archives twice. Never reuse old checksums with a different source HEAD.
3. Record `CANDIDATE_HEAD=$(git rev-parse HEAD)` and confirm empty `git status --short`,
   independent root history, no alternates/private refs, new duck and updated frames.
   Run the recorded candidate checks and `bash scripts/scan-secrets.sh`. Check staged
   build-info.source_head equals this HEAD; verify all artifact checksums.
4. Create/select the approved GitHub repository named SHMAX, initially private for
   staging if desired. Future command template:
   `gh repo create "$APPROVED_ORG_LOGIN/SHMAX" --private --description "Self-hosted gateway connecting MAX and Telegram through a Telegram bot, forum topics and controlled user access."`
   Verify remote before adding `origin "$REPO_URL.git"`. No private development refs.
5. Future command: `git push -u origin main`. Inspect every GitHub Actions job;
   local results are not evidence of GitHub CI passing. Stop on any failure.
6. GitHub Private Vulnerability Reporting requires the public-repository stage per
   GitHub's documentation. After the approved source is ready and public visibility
   is separately authorized, enable it in Settings → Security and quality → Advanced
   Security. Verify Security → Advisories has Report a vulnerability, maintainer access
   and notification preferences. Do not advertise an active channel while pending.
   Confirm the actual reporting route in SECURITY.md using verified REPO_URL. Make
   that approved documentation commit in the candidate, run checks and rebuild from
   its new clean HEAD before tagging; upload/push it only under publication authorization.
7. Configure description/topics from GITHUB_PRESENTATION.md. Verify public RU/EN
   README, all ten duck screenshots, license and local links in a logged-out browser.
8. Refresh `CANDIDATE_HEAD=$(git rev-parse HEAD)` after the security-policy commit.
   Confirm no v0.1.0 tag exists and this final HEAD matches rebuilt build-info.
   Future commands: `git tag -a v0.1.0 "$CANDIDATE_HEAD" -m "SHMAX v0.1.0"`,
   `git push origin v0.1.0`. Never overwrite an existing tag. Inspect packaging Actions.
9. Combine approved RU/EN notes into a temporary release body. Future command:
   `gh release create v0.1.0 --draft --verify-tag --title "SHMAX v0.1.0" --notes-file "$NOTES_FILE" "$ASSET_DIR/shmax-v0.1.0-source.tar.gz" "$ASSET_DIR/shmax-v0.1.0.tar.gz" "$ASSET_DIR/SHA256SUMS" "$ASSET_DIR/ARTIFACT_SHA256SUMS" "$ASSET_DIR/release-manifest.json" "$ASSET_DIR/build-info.json" "$ASSET_DIR/validation.json" "$ASSET_DIR/RELEASE_NOTES.ru.md" "$ASSET_DIR/RELEASE_NOTES.en.md"`.
   Verify draft contents and owner approval, then publish the draft through GitHub UI.
   CI itself never creates a GitHub Release or publishes Docker/packages.
10. Download the published assets into a new directory outside both repositories.
    Verify SHA256SUMS and ARTIFACT_SHA256SUMS, manifest and source HEAD; extract both
    archives, verify installer execute mode, migrations/hostd/systemd/frontend/duck.
11. Verify actual release/download URLs and archive installation instructions from
    a logged-out browser. No invented remote-shell one-liner. Report the real repo,
    tag, release and checksum links; announcements remain text until separately posted.

No production Debian/SSH or live MAX/Telegram access is part of this procedure.
