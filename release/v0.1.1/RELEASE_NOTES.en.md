# SHMAX v0.1.1 — MAX Photo Album Fix

Prepared stable patch; publication awaits owner approval.

Fixed multiple supported attachments in one MAX message. The bridge previously
fetched photos but retained only the first, turning remaining attachments into
unsupported notices before calling Telegram.

Bot API now sends consecutive photos in groups of 2–10, preserving order, forum
topic, attribution and replies. Single photos keep their existing path. Documents
and other supported media are sent separately. Adapters without groups, and
explicit Bot API group rejections, use ordered single-item fallback. Ambiguous
network timeouts never trigger blind group-to-single fallback.

Durable queue checkpoints retain confirmed delivery progress across retries and
restarts. Every returned Telegram ID resolves to the original canonical MAX
message. An additive alias-table migration preserves existing records.

Transient download failures use the existing bounded retry policy (8 attempts).
After exhaustion, remaining valid media are delivered and partial failure remains
visible in the queue. Expired photo URLs can be refreshed with the actual PyMax
get_messages API using source chat/message/photo identifiers. chat_id=0 stays valid.

Telegram offers no idempotency key. Remote acceptance followed by a lost response
or a crash before durable checkpoint can still duplicate on retry. Exactly-once
remote delivery and per-item partial sendMediaGroup responses are not claimed.

No sanitized live six-photo payload was available. Tests use actual PyMax 2.4.1
PhotoAttachment/Message models; unknown gallery variants and live messenger
compatibility remain unverified. The offline summarize-max-attachments.py tool
outputs safe metadata without URLs, tokens, IDs, names or message text.

Existing users only need to update:

```bash
sudo shmax update /path/to/shmax-v0.1.1.tar.gz
```

No reinstall, onboarding, bot token reset, MAX reauthorization or Xray reselection
is required. The signed one-line installer continues to select the actually
published stable release, currently v0.1.0 until v0.1.1 is approved and published.
