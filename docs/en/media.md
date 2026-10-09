# Photos and stickers

Telegram → MAX selects the largest aiogram PhotoSize by area/size and uses `file_id`, never `file_unique_id`. `getFile` and streaming download use the same configured Bot API session: Direct, SOCKS5, HTTP or internal Xray SOCKS, without direct fallback. Validated images receive their actual format extension and go through PyMax `Photo(path=...)`, with caption and native reply ID. Custom attribution appears once, e.g. `[Роман]: Вот документ` with an actual photo. Text fallback is not successful media delivery. MAX → Telegram photos retain the existing delivery/author path.

| Source | Transfer |
| --- | --- |
| Telegram static WebP | Transparent PNG photo in MAX |
| MAX static image sticker | Native static PNG Telegram sticker when within 512 px/512 KiB requirements; actual photo otherwise |
| Telegram animated TGS or video WebM | Static thumbnail where available; one readable fallback without a thumbnail |
| MAX animated with usable raster URL | First frame/static representation; fallback for unsupported raster/Lottie-only |

Full TGS/WebM animation conversion is not supported; no FFmpeg/Lottie stack is added. Existing Pillow and ARMv7 codecs run in a separate process: 8 seconds wall time, 5 seconds CPU, 256 MiB Linux address space, 4 MP sticker/25 MP photo. Input stickers are capped at 5 MiB; downloads at 20 MiB/35 seconds. Only JPEG/PNG/WebP are decoded. Empty/HTML/oversized/invalid files are rejected and cleaned. MAX downloads require HTTPS on trusted MAX/CDN origins, with redirects disabled.

Queue payload distinguishes MEDIA_PENDING from MEDIA_FALLBACK; actual attachment send reports MEDIA_DELIVERED, failure MEDIA_FAILED. A delivered text fallback never claims media success. Retry cache lifetime and completion/TTL cleanup remain. Diagnostics include direction/category/operation/type/fixed sanitized reason/correlation, excluding authenticated URLs, tokens, proxy passwords, subscriptions, binary data and SecretStore contents. Delivery remains at-least-once; a mapping-write failure after external send can still require retry.


### PyMax 2.4.1 upload compatibility

The SDK reads `photoIds` from the initialization URL before uploading. SHMAX uses a per-client compatibility wrapper: the signed URL stays unchanged; a missing query ID is accepted only when the actual upload response contains exactly one validated photo/token. Existing query IDs must match the response. Malformed or ambiguous responses fail without sending a message. Profile photos and other upload methods retain the SDK implementation. The reason the live server omits the query remains unverified.

`document:` and `file:` references both use SDK `File`, preserving the original filename separately from the cache path. Saved Messages (`chat_id=0`) is a valid destination for text, photos, documents, converted static stickers and replies.

Transient failures retain bounded retries (eight attempts); unsupported schema and authentication/session failures retain the queue row and cache for manual review. Lost final confirmation after upload stops automatic resend with `delivery_confirmation_uncertain_review_before_retry`: check MAX before retrying. Manual retry resets attempts on the same row and idempotency key. This does not remove the existing at-least-once limitation after external delivery and failed mapping persistence. Upload diagnostics contain operation, known schema names, query-ID presence, status and correlation only; SDK media logs are redacted within the current task context.
