# Restore and migration

1. Create a [portable encrypted export](backups.md) on the source; keep the single .shmax file and password separately.
2. Stop the source SHMAX. Two instances must not poll the same Telegram Bot/operate the same MAX account concurrently. Migration does not revoke source credentials.
3. Install SHMAX on the new Debian host and create the destination owner through the normal setup link.
4. On “Как настроить SHMAX?”, choose “Восстановить из резервной копии”. No MAX or Telegram setup is required first.
5. Upload the file, enter its password and inspect version/date/contents/warnings. Active data remains unchanged before confirmation.
6. Confirm that the source is stopped and start restore.
7. After restart, log in with the destination owner's credentials. Open the dashboard without repeating steps 2–10; verify routes, users, signatures, both directions and reboot.

The destination owner becomes the only web owner regardless of imported ID/username collisions. Imported web users, sessions and setup tokens are removed and cookie-signing secret rotated. Telegram user ACL/roles/approval/blocking/signatures are retained; unknown participants are not auto-approved. Completed queue rows, mappings and idempotency keys remain. Pending/retry/processing rows are quarantined as failed with `migration_review_required`; inspect before retry because the source may already have sent them. Media cache is excluded.

## Validation and transaction

Administrative authentication, CSRF, owner privilege, upload caps and rate limits are server enforced. Full-file authentication precedes staging extraction. Manifest hashes/sizes, paths/types, SecretStore recovery key, SQLite integrity, known Alembic revision and application compatibility are checked. Symlinks, hardlinks, traversal, special files and decompression bombs are rejected. Newer application versions/unknown schema revisions are rejected; supported previous revisions migrate forward without downgrade.

Hostd receives only a random job ID, never an arbitrary command/path/password. It revalidates, migrates staging, stops the web container, preserves previous persistent directories, installs staged data with destination UID/GID, starts the container and verifies host-side `/health/ready`. Web never receives the Docker socket. An fsynced journal tracks swaps; failed startup/readiness rolls back. Interrupted swaps are recovered on hostd startup.

Wrong password/corruption returns “Неверный пароль или повреждённая резервная копия” without modifying active data. Failed rollback explicitly reports `rollback_failed`; preserve /var/lib/shmax, `previous/` and the journal for owner recovery.

Destination bind/IP/port/environment/hardware binaries remain local. Available MAX sessions and Telegram/Xray configuration are restored, but external services can revoke authorization. Reauthenticate only the affected integration; other configuration/routes/users remain. `/health/ready` covers DB/storage readiness, followed by owner live bridge checks.

## Legacy local recovery

CLI `shmax restore <archive>` is low-level extraction: create a separate safety copy and stop the application first. It does not provide the new onboarding/hostd transaction. The legacy in-process restore API is blocked in production (HTTP 409); use portable v2 UI for new migration.

Safe Debian diagnostics:

```sh
systemctl status shmax-hostd.service shmax-boot.service
journalctl -u shmax-hostd.service --since '-10 minutes' --no-pager
stat -c '%u:%g:%a' /run/shmax/hostd.sock
```

Never publish archives, passwords, tokens, subscription URLs, API Hash or SecretStore keys.

## Previous-release backups in v0.2.0

The owner restore UI accepts portable v2 and original local v1 gzip-tar/SHMX1
backups. The contents determine the format. Enter the original password for
encrypted v1 (including eight-character passwords); leave it blank for plain
v1. Full authentication/checksums and schema validation precede conversion into
private portable staging. The existing hostd restore transaction, safety
snapshot, rollback and destination-owner rebinding remain in effect. Pending
source deliveries remain quarantined for review rather than replayed on a new
host. A missing SecretStore key or corrupt database is rejected without
changing active data. Updater cold snapshots without an application manifest
remain a separate offline rollback format.
