# SHMAX v0.1.1 — published release record

[SHMAX v0.1.1](https://github.com/SHMAX-Project/SHMAX/releases/tag/v0.1.1)
was published on 2026-10-12 from the independently reviewed public commit
`9dbddb8d161cc100e2bdfaa54c66d8833eff0b95`.

The release tag and assets are final. Later documentation corrections on `main`
do not change that release. Do not recreate its tag, replace its assets or rewrite
public history. Future software fixes belong in a new release.

The [latest stable release](https://github.com/SHMAX-Project/SHMAX/releases/latest)
is selected by the signed one-line installer. Existing installations can update with:

```sh
sudo shmax update
```

Or use the published production archive:

```sh
sudo shmax update /path/to/shmax-v0.1.1.tar.gz
```

Release assets include production/source archives, the `shmax` launcher,
`SHA256SUMS`, `SHA256SUMS.sig`, the release manifest, build information,
installer, public verification key and Russian/English release notes.
The private signing key remains outside the repository and release artifacts.

The signed packages passed inventory, source-commit and installer verification.
Upgrade and rollback were checked in an isolated Debian 13 amd64 VM with
synthetic persisted fixtures. See the [changelog](../../CHANGELOG.md) and
[release notes](RELEASE_NOTES.en.md) for confirmed changes and validation.
