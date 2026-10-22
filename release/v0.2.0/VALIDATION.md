# v0.2.0 validation / Проверки

Validated runtime commit: `d717f243eba0245aa083823c5112d68ecabfbbda`.
Documentation publication may use a later commit; the final production archive must have identical application files to the deployed, signed candidate.

- Backend core run: 1049 passed, 4 environment skips; 15 real-Xray runtime checks passed separately. Later targeted host-helper/migration/album checks passed. These are separate runs, not a claimed single aggregate run.
- Frontend: 58 tests passed, TypeScript and production build passed.
- Desktop/mobile: complete ten-step onboarding, 18-page layout audit per viewport and new v0.2.0 controls passed (6 browser cases). Encrypted export/new-owner restore/login also passed separately.
- Albums/media/destination fanout: 74 targeted regressions passed, including captionless albums, per-destination mappings and retries.
- Historical backups: actual writers from immutable v0.1.0–v0.1.3 tags produced plaintext and encrypted v1 archives; all 8 import cases passed.
- Existing Debian 13 amd64 deployment: 164 isolated acceptance tests passed, 1 historical-Git test skipped because the packaged container has no Git history (the same test passed locally).
- Signed upgrade and migration to `0012_multi_group` passed; SQLite integrity and foreign keys passed. Routes, grants, saved sessions, existing delivered/failed queue records and secret permissions were preserved. Historical failed messages were not replayed.
- The separate host helper reports v0.2.0 and has matching migration files. Published HTML/CSS/JavaScript match the signed package.
- Component restart and full VM reboot verified automatic startup, host-address readiness, MAX/Telegram recovery and persistent data. A site-specific persistent-address correction was required after DHCP changed the address; v0.2.0 does not reserve DHCP addresses automatically.
- No real test messages were sent during network/media acceptance. Actual delivery of every external provider/media combination is not claimed.

## Security / Безопасность

Public source, full Git history and final archives are checked with the pinned secret scanner and reviewed fixture fingerprints. Signing uses the existing trusted private key outside the repository; only its existing public verification key is distributed. No private signing/SSH keys, sessions, production databases, configuration backups or deployment reports belong in the release. Server backups stay root-only on their original host.

Archive integrity is recorded in `SHA256SUMS` and authenticated by `SHA256SUMS.sig`. Additional documentation/validation sidecars are not implicitly covered by that signature; `ARTIFACT_SHA256SUMS` is an inventory, not a separate signature.

## Limits / Ограничения

One MAX account; Telegram membership controls reading, group grants control sending. Metrics are in memory for at most one hour. An uncertain external delivery result cannot guarantee exactly-once delivery. Old unfinished Telegram jobs without sender identity require review; cross-server restore quarantines unfinished source jobs. ARM64/ARMv7 remain experimental build targets without completed physical acceptance. Use a verified pre-upgrade backup for schema rollback; do not rewrite previous releases.
