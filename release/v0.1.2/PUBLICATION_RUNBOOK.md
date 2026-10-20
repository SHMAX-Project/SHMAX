# v0.1.2 publication runbook

Release date: 2026-10-20. Owner-authorized publication; production deployment is a separate operation.

- Preserve the published v0.1.0/v0.1.1 tags and assets. Keep private history, deployment addresses, backups, credentials and private signing keys out of GitHub.
- Build from the clean public commit: `bash scripts/build-release.sh v0.1.2 OUTPUT`.
- Verify `python scripts/check-release.py OUTPUT`; sign with the existing `scripts/sign-release.py OUTPUT --key PRIVATE_KEY_PATH`. The key remains outside the repository/staging and never goes to production.
- Record source commit and application-code equivalence to the tested candidate if only publication documentation changed. Do not label untested code as tested.
- Stage source/production archives, launcher, manifest/build-info, signed SHA256SUMS, installer, existing public verification key, changelog, RU/EN notes and sanitized validation. Keep private commit/server metadata in local reports only.
- Fast-forward the public repository; create the immutable v0.1.2 tag. Create a draft Release, upload/verify all assets and publish stable/latest after the checks. Confirm GitHub public downloads, checksums, signature and latest version.
- Update repository description, README badges/features, documentation indexes and support guidance. Historical release directories remain historical.

Runtime acceptance: 1002 backend passed / 9 local skips, 51 frontend passed, 26 browser passed; Debian 13 amd64 signed clean install, v0.1.1 upgrade, forced cold rollback, real Docker journal persistence, 38 recovery/journal/queue/TG regressions and 5 real-Xray validations passed. See release notes for limitations and synthetic-test scope.
