# v0.1.3 candidate, no publication or production deployment

Build from the reviewed, clean PUBLIC commit: `bash scripts/build-release.sh v0.1.3 OUTPUT`.
Validate with `python scripts/check-release.py OUTPUT`, then use the existing
`scripts/sign-release.py OUTPUT --key PRIVATE_KEY_PATH` outside the repository.
Never copy the key into staging, containers or archives; retain the committed public key.

Keep private development history separate. The public candidate must descend from
published public main, with no private commits merged. Record both commit SHAs in
an external validation report. Existing tags/assets must remain unchanged.

Stage the standard installer, public verification key, changelog and RU/EN notes.
Verify signature, package inventory and checksums; record exact tested archive hashes.
Run backend/frontend/browser and isolated Debian upgrade/rollback acceptance.
Do not create/push a tag, PR, GitHub Release or deploy without a separate instruction.
