# Chats and routing

One selected MAX chat ↔ one Telegram topic. Routes persist in the
DB and can be toggled or re-created. Replies: a reply in a topic
goes to the originating MAX chat and vice versa; the message_map
table plus the outbound native-id registry provide idempotency and
echo suppression without any visible markers. Unknown forum topics
are dropped (fail-closed).
