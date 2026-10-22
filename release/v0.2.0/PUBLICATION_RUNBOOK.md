# v0.2.0 publication

1. Preserve the existing signed deployment artifacts and immutable v0.1.0–v0.1.3 releases. Finalize RU/EN documentation and commit only public material.
2. Validate documentation links, screenshot hashes/privacy metadata and full-history secret scan with the pinned scanner. Review every finding without printing secret values.
3. Build from clean committed HEAD using `bash scripts/build-release.sh v0.2.0 OUTPUT`; check the package and compare application-file bytes/modes against the deployed signed candidate.
4. Sign `SHA256SUMS` with the existing trusted release key outside the repository. Verify against `deploy/bootstrap/release-public.pem`; never copy or print the private key.
5. Inspect both archives and all sidecars for private material. Publish sanitized validation only; server reports and backups are excluded.
6. Push a fast-forward main update and a new immutable v0.2.0 tag. Create a draft release, upload and verify every asset before publishing as stable/latest. Do not replace older tags/assets.
7. Check main/tag CI, public download signatures, checksums and package structure, then compare historical release/tag metadata with the pre-publication inventory.

See [validation](VALIDATION.md), [RU notes](RELEASE_NOTES.ru.md), [EN notes](RELEASE_NOTES.en.md) and [release process](../../docs/en/release-process.md).
