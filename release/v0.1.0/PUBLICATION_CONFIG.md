# Publication-time configuration — single source of owner information

Original requested login **SHMAX** was occupied. Owner approved **SHMAX-Project**.
Organization and empty public repository were created and verified on 2026-10-10
under `viacheslav-fomenko`. Repository: https://github.com/SHMAX-Project/SHMAX .
Code, tags and release artifacts have not been uploaded.

APPROVED_ORG_LOGIN: `SHMAX-Project`.
REPO_URL: `https://github.com/SHMAX-Project/SHMAX`.

Resolve these variables once in the publication session:

- `APPROVED_ORG_LOGIN`: verified organization login controlled by the owner.
- `REPO_URL`: `https://github.com/${APPROVED_ORG_LOGIN}/SHMAX`.
- `PUBLIC_AUTHOR_NAME` / `PUBLIC_AUTHOR_EMAIL`: explicitly approved public commit
  identity, preferably verified GitHub noreply identity rather than personal email.
- `CANDIDATE_HEAD` / `ASSET_DIR`: verified clean candidate commit and staged artifacts.

Current local root identity is explicitly a publication placeholder:
`SHMAX publication staging (placeholder)` / `publication-placeholder@shmax.invalid`.
It represents local snapshot preparation, not attribution of all code to a person.
Existing source/third-party attribution is preserved. Owner may approve retaining
this safe placeholder or explicitly approve replacing it in the separate public
candidate before publication; never change private history. If replaced, new HEAD
requires re-scan/rebuild and new checksums before any push/tag. Do not invent a real
person, personal email or GitHub account. These are final owner/publication variables.

README uses relative links; do not scatter hardcoded owner URLs. After confirmation,
update the real reporting route in SECURITY.md, configure repository description/
topics from GITHUB_PRESENTATION.md, and use REPO_URL for the actual remote/release.
Verify the final URL and installed download links; no remote shell URL is claimed now.
