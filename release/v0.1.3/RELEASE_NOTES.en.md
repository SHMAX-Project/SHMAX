# SHMAX v0.1.3 — connection recovery and persistent event journal

Patch candidate; publication and deployment require separate owner approval.

- Fix the complete Xray selection path with an existing bot token. Serialize switching against Telegram recovery; distinguish network application, adapter installation and rollback. Attempt to restore the previous running node on failure. No direct fallback or weakened TLS.
- Recover MAX temporary disconnects with the saved session through one lifecycle owner, bounded timeouts and exponential backoff with jitter. Recovery cannot initiate SMS, logout or relogin; revoked authentication requires operator action.
- Rebind one MAX event consumer to each connection generation and reject obsolete SDK callbacks. Include listener liveness in health; distinguish verification unavailable from authentication rejected.
- Add an administrator-only persistent event journal with filters, search, cursor pagination, details and ten-second refresh on the visible journal page. Store metadata only, never message text or credentials.
- Bound retention by 90 days, 100,000 records and a 128 MiB storage budget. Use an isolated SQLite database, bounded producer queue, off-loop batching and incremental cleanup. Aggregate repetitive connection failures while retaining individual delivery correlation.
- Drain Xray stderr asynchronously, exposing fixed safe categories only.
- Reject empty outbound payloads and stop retrying identical permanent Telegram 400/401/403 errors. Existing failed production deliveries are never replayed or reset automatically.

Preserve v0.1.1 photo albums, all message ID aliases, checkpoints, replies/topics, ACLs, Saved Messages chat_id=0 and encrypted sessions. The historical MAX media failures remain insufficiently detailed to attribute to one cause.

Upgrade using the standard signed package and `sudo shmax update /path/to/shmax-v0.1.3.tar.gz` after approval. The updater creates a cold persistent-data backup and restores the previous image/data when readiness fails. A configuration-only backup does not replace that full snapshot. Upgrading from v0.1.1 must not require reauthorization.
