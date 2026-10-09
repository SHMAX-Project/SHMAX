# Release process

v0.1.0 is prepared locally, unpublished. Publication requires the owner's exact
instruction “А ТЕПЕРЬ ВЫКЛАДЫВАЕМ” and resolution of the audit blockers.

From a clean committed HEAD run `bash scripts/build-release.sh v0.1.0 [output-directory]`.
Requires Python 3.12+, Node >=20, npm, Git and npm registry/cache access.
The builder exports HEAD, checks VERSION and runs npm ci/build in the isolated
export. It never consumes working frontend/dist or creates tags/pushes.

Outputs: `shmax-v0.1.0-source.tar.gz` (no frontend/dist), `shmax-v0.1.0.tar.gz`
(with production frontend/dist and root compose.yaml), SHA256SUMS,
release-manifest.json (`{version, channel, artifact, sha256}`), build-info.json
(source HEAD, date, sizes and dependencies). Tar/Gzip timestamps and ownership
are normalized using the commit timestamp; repeat builds of one HEAD must yield
identical archives. The changing build-date sidecar is outside the archives.
Updater manifest semantics are preserved.

Run `python scripts/check-release.py <output-directory>`, then extract outside
the checkout and verify backend import/version, installer permissions and frontend.
Rebuild after any HEAD change. Actions packaging validates and stores runner
artifacts, never creates a GitHub Release. Actions have not run on GitHub yet.

[Future publication runbook](../../release/v0.1.0/PUBLICATION_RUNBOOK.md) ·
[Local publication status](../../release/v0.1.0/PUBLICATION_STATUS.md).
Production updates use verified releases through UI/hostd, not git pull.
