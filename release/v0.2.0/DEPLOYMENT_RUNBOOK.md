# v0.2.0 deployment

Build committed clean source with `scripts/build-release.sh v0.2.0 OUTPUT`; check archives and sign with the existing trusted private key through `scripts/sign-release.py`. The private key stays in the trusted local environment.

Verify signature and SHA256 on the existing server. Build and test a disposable candidate image with isolated synthetic data before touching the running container. Include photo albums, multi-group routing, migration, backups, MAX networking and pinned Xray validation.

Use the standard local release updater transaction. It preserves the prior image, backs up deployment files, creates and verifies a cold data snapshot on the server, applies migration and checks application plus both integrations. Backup directories must be root-owned 0700 and backup archives 0600; never download server configuration, sessions or backup archives.

After acceptance verify schema, SQLite integrity and foreign keys, preserved groups/routes/user grants, queue state, album mappings, selected network paths and restart count. Retain the prior image and verified snapshots for rollback. Returning to schema 0011 requires the pre-upgrade cold backup.

The owner authorized updating the existing server after design and album checks on 22 October 2026. GitHub publication is not part of this deployment.
