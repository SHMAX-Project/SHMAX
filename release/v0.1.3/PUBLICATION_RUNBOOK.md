# v0.1.3 publication runbook

Build the clean committed source with `bash scripts/build-release.sh v0.1.3 OUTPUT`, verify with `scripts/check-release.py` and sign with the existing trusted release key using `scripts/sign-release.py`. Never include private keys or server data.

Run backend/frontend checks and isolated Debian upgrade/readiness/rollback checks. For this release the owner explicitly authorized production deployment first, followed by GitHub publication only after successful production verification. Use the same signed archive for deployment and publication. Retain all published tags and assets of v0.1.0/v0.1.1/v0.1.2 unchanged.

Publish the fast-forward source commit and immutable v0.1.3 tag. Upload artifacts into a draft release, verify checksums, signatures and downloads, then publish stable/latest. Verify CI and anonymous public downloads. See RELEASE_NOTES.ru.md and RELEASE_NOTES.en.md for changes and limits.
