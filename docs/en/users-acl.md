# Users and ACL

Fail-closed: Telegram users cannot write to MAX until approved.
Per-user approve/block/role (owner/admin/operator/read_only) and
nickname (used for author attribution). The MAX side is checked
too (view_chat). Blocked and read-only users cannot send.


## Member synchronization

To import existing silent members, save optional API ID/API Hash from
my.telegram.org on Telegram. API Hash is secret. SHMAX uses the existing bot,
without a human userbot. On Users click “Синхронизировать с Telegram” and
check the result. Members need not message the bot when full sync is configured.
Future membership changes use chat_member; visible messages remain a fallback.
Missing configuration and sync failures do not stop the bridge.

New members are unapproved and read_only; Telegram admin grants no SHMAX admin.
Existing ACL and nickname are preserved. See
[Telegram group](telegram-group.md#automatic-participant-discovery).

## Signature in MAX

On a Telegram user card, an administrator can edit “Подпись в MAX”, check the preview and save it. This uses existing `nickname`, a SHMAX-local author override, independent of the Telegram profile and role. For example, “Анна” produces `[Анна]: Клиенту уже ответили.`

Clear restores the Telegram display name, then @username or the safe fallback. Telegram → MAX text, replies, media captions and text fallbacks receive exactly one prefix. MAX → Telegram author resolution remains unchanged.

Values are trimmed; empty values clear the override. Cyrillic, Latin, emoji and ordinary punctuation are allowed, up to 64 Unicode characters. Newlines and control characters are rejected. Mutation requires an administrative session and CSRF and changes no approval, blocking, role or membership. Member sync, Telegram profile renames and Telegram-admin changes never overwrite the local signature.
