# v0.1.1 publication preparation

This patch is pending owner approval. Do not change the published v0.1.0 tag or assets.

Build from the clean public publication commit with `scripts/build-release.sh v0.1.1 OUTPUT`.
Validate with `scripts/check-release.py OUTPUT`; sign the checksum inventory offline
with `scripts/sign-release.py OUTPUT --key PRIVATE_KEY_PATH`. The signing key must
remain outside the repository, package, test VM and reports.

Copy the current `install.sh`, committed `deploy/bootstrap/release-public.pem` and
both release notes into the staging directory. Preserve the existing bootstrap
contract: production/source archives, launcher `shmax`, `SHA256SUMS`,
`SHA256SUMS.sig`, release manifest and build information.

Before approval, verify the actual production image and upgrade an isolated Debian
13 amd64 installation made from the signed, published v0.1.0 assets. Exercise failed
candidate readiness and cold rollback; retain sanitized diagnostics before rollback.
Record the source commit, archive hashes, test results and acceptance evidence in the
owner's external report. Do not put runtime data, session files, VM keys or raw payloads
in the public source or artifacts.

After publication is explicitly approved, the owner can create `v0.1.1` at the reviewed
public commit, push that commit/tag and publish the verified assets. Those operations
have not been performed by this preparation. The current latest bootstrap still installs
published v0.1.0 until the new release is actually published.

Existing installations use one command once the verified package is on the host:

```sh
sudo shmax update /path/to/shmax-v0.1.1.tar.gz
```

No reauthorization or manual database operation is needed. Live messenger acceptance
requires an actual six-photo MAX message; SDK-model tests alone do not establish all
possible live gallery representations. `scripts/summarize-max-attachments.py` reads a
privately saved message JSON from stdin and emits only ordinal/type and bounded
shape metadata, excluding message text, IDs, URLs, credentials and arbitrary fields.
Never upload the raw JSON or save it in Git.
