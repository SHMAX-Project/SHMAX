# Release process

Current stable release: [v0.1.3](../../release/v0.1.3/RELEASE_NOTES.en.md). Publication and production deployment are separate actions requiring explicit owner authorization. Historical v0.1.0/v0.1.1 documents describe those releases; never replace their tags or assets.

1. Commit a clean public HEAD with consistent VERSION, Python/frontend metadata and documentation. Exclude private history, server data, backups and signing keys.
2. Run `bash scripts/build-release.sh v0.1.3 OUTPUT` with Python 3.12+, Node >=20, npm, Git and registry/cache access. It exports committed source and runs npm ci/build in isolation, never using working dist/node_modules.
3. Run `python scripts/check-release.py OUTPUT`; verify source/production contracts and the tested runtime. Documentation-only finalization must preserve application files; code changes need relevant tests.
4. Sign with `python scripts/sign-release.py OUTPUT --key PRIVATE_KEY_PATH`. Keep the private key local and retain the existing public verification key.
5. Complete backend/frontend/browser and isolated Debian upgrade/rollback/bootstrap checks. Stage RU/EN notes, installer, public key, changelog and validation without private/server metadata.
6. With owner authorization, publish source, an immutable tag and draft Release; upload/verify assets, then publish stable/latest. Verify public download, signature, manifest and source commit. Deploy separately through the updater, never git pull.

Outputs: `shmax-v0.1.3-source.tar.gz` without frontend/dist; `shmax-v0.1.3.tar.gz` with frontend/dist; signed `SHA256SUMS`, `release-manifest.json` and `build-info.json`. Tar/Gzip timestamps and ownership use the commit timestamp. The changing build-date sidecar is outside the archives. Actions packaging validates/stores runner artifacts without publishing a Release.

[v0.1.3 runbook](../../release/v0.1.3/PUBLICATION_RUNBOOK.md) · [Testing](testing.md) · [Updates](updates.md).
