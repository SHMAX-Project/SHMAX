# Groups and administrator overview (v0.2.0)

Use separate private Telegram forum groups for separate teams. Pair the same
bot with each group; existing groups remain connected. In group settings choose
the group and map MAX chats to new or existing topics. One topic has one MAX
chat; the same MAX chat may be assigned to several groups.

Edit a mapping's name/topic, disable individual mappings or disable the whole
group. To transfer a chat, add its mapping in the new group and disable the old
one. Previously delivered Telegram history remains in its original group.

Choose a group on Users before granting send access. Explicit approval and a
send-capable role are required independently per group. Discovery and Telegram
admin status do not grant SHMAX access. Global blocks and applicable explicit
denies remain effective. Telegram membership controls who can read group
history: revoke Telegram membership to revoke reading, not merely SHMAX sending.

Enable administrator overview and automatic topic creation in a trusted forum
group. Multiple overview groups are supported. The first new MAX message creates
a topic; old history is not backfilled. Overview includes private MAX chats and
confirmed Telegram → MAX messages. An admin reply goes to that MAX chat and its
other linked groups. Disabling automatic addition preserves existing mappings;
turn off the whole group or individual mappings to stop them.

Configure chat and actual-sender labels per destination. Unknown senders use
neutral text rather than the owner's name or a phone number. Label settings
apply to new messages; retries retain the queued label. Each destination owns its
queue row, retry state and mapping. Workers revalidate current routes/groups and
send grants; mirrors wait for the original MAX confirmation. A lost Telegram
create-topic response may leave an unused topic because the API has no
idempotency key.
