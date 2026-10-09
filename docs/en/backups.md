# Backups

## Local recovery

“Создать локальную копию” creates an on-device recovery archive containing SQLite, configuration, state, sessions and SecretStore **including its master key**. Ordinary local backups are not encrypted as a whole. Mode 0600 is access control, not encryption. Never attach these archives to support tickets or public issues.

## Portable encrypted export

On Backups, under “Перенос на другой сервер”, enter and confirm a password (10–256 characters) and click “Экспортировать для переноса”. Download one `shmax-backup-YYYY-MM-DD.shmax`. The password is required, is never persisted server-side or in localStorage, and is cleared from the form after the operation. Keep it separately; forgotten passwords cannot be recovered.

The encrypted package contains a consistent SQLite/WAL snapshot (settings, routes/topics, users/ACL, custom MAX signatures, queue/idempotency and reply/native-ID mappings), required SecretStore keys, configuration, available MAX/Telegram sessions and persistent application state. Selected Xray nodes/subscription credentials are recovered from SQLite/SecretStore. Logs, media cache, temporary files, other backups, Docker images, Python/Node environments and hardware binaries are excluded. Destination `/opt/shmax/.env` and compose remain destination-specific; the old LAN IP is not imported.

Production hostd briefly pauses the web container while snapshotting and unpauses it in cleanup. SQLite uses the backup API, including WAL; secret/session files come from the same frozen appliance state. Encryption and full authenticated decryption/inventory verification use disk staging before the download. Unavailable hostd fails export.

## Format and limits

Portable v2 uses Argon2id (32 MiB, 3 passes, parallelism 1), AES-256-GCM and fresh random salt/nonce prefix. Records are at most 1 MiB; authenticated header/index/length plus a required authenticated end record prevent reordering and truncation. Only magic/version, salt and nonce prefix are public. Application/schema version, date, inventory, sizes and SHA256 are encrypted. Fixed KDF parameters cannot be increased by hostile archives.

Limits: 512 MiB package/extracted data, 128 MiB per file including the SQLite snapshot, 10,000 files, 1 MiB manifest. Allow disk space for snapshot and verification staging. SQLite/JSON/ordinary files are architecture-neutral; amd64 ↔ ARMv7 migration requires compatible application/schema versions. Actual execution on both architectures remains live acceptance.

Temporary exports are removed after transmission. Abandoned uploads/previews older than 24 hours are cleaned during subsequent operations. `backups/portable/<id>/previous/` is retained until explicitly removed by the owner; it is unencrypted local safety state and excluded from future portable exports.

Legacy local v1/CLI archives are not portable v2 and are rejected by the new import screen. Create a new UI export for migration. See [Restore](restore.md).
