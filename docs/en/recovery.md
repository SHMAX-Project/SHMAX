# Recovery

Priority: data first, then UI access, then app version. Failed
update → check the rollback result; if successful, the previous version is healthy.
If rollback also failed, preserve data, collect diagnostics and follow the reported
manual recovery instructions.
Broken publication → recreate. Broken app → `install.sh --mode
repair` (data preserved). Configuration rollback → restore a
backup (safety copies are automatic). Last resort:
`--mode uninstall` keeps data; `--purge` DESTROYS it — never as a
routine fix.
