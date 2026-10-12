# SHMAX v0.1.1 — MAX → Telegram photo albums

Fixed multiple photos from one MAX message: previously only the first photo reached Telegram, while the remaining photos were marked as unsupported attachments.

- Native Telegram albums of 2–10 consecutive photos preserve order, captions, attribution, forum topics and replies. Single photos retain their existing path; other supported attachments are sent separately.
- Durable queue checkpoints retain confirmed progress across retries and restarts. All album message IDs resolve to the original MAX message, including replies to the last photo.
- Bounded download retries and expired photo URL refresh through PyMax. After retry exhaustion, remaining valid attachments are delivered and partial failure stays visible in the queue.
- Improved 0.1.0 → 0.1.1 update compatibility: an additive migration preserves data; no reinstall or repeated setup is required.

Validation: backend — 985 passed / 4 skipped; frontend — 46 passed; typecheck — PASS; 24 album regression scenarios using PyMax 2.4.1 models. Upgrade and rollback passed in an isolated Debian 13 amd64 VM, preserving settings, routes, queue/history, encrypted secrets and synthetic session files. Final archives were checked against the public commit, their inventories and the signed installer's verification/extraction path.

Update an existing installation:

```bash
sudo shmax update
```

Or update using the downloaded production archive:

```bash
sudo shmax update /path/to/shmax-v0.1.1.tar.gz
```

Telegram offers no idempotency key: a lost response after remote acceptance or a crash before a durable checkpoint can still duplicate a send on retry. Ambiguous timeouts never trigger blind album-to-single fallback.
