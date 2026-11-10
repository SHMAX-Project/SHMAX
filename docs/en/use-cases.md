# Three ways to use SHMAX

[Keep MAX off participants’ phones with a server or VPS](max-without-phone.md).

[Install](installation.md) · [Setup wizard](first-setup.md) · [Русский](../ru/use-cases.md)

SHMAX connects **one MAX account** to your Telegram bot and one or more private
Telegram groups. It is not a public forwarding service: you run the gateway on
your own server and choose who can use it.

## 1. They use MAX; you reply in Telegram

**Situation:** some contacts use MAX, while your day-to-day workspace is Telegram.
You would like to avoid switching apps for every conversation.

**Set it up:**

1. Install SHMAX and connect MAX and Telegram in the [wizard](first-setup.md).
2. Create a private Telegram group with topics and connect it to SHMAX.
3. Select the MAX chats you need. Map each to its own Telegram topic.
4. Approve the participants who may reply to MAX through SHMAX.
5. Send a test message from MAX. Reply in its Telegram topic and check that
   the response arrives in the original MAX chat.

**Result:** conversations stay separate by topic. Contacts keep using MAX;
you reply from Telegram. Attachments follow the
[supported types and size limits](media.md).

## 2. Separate groups for separate teams

**Situation:** support needs some conversations; another team needs others.
Not everyone should see every conversation.

**Set it up:**

1. Create a separate private Telegram group for each team. Enable topics,
   add your bot and give it the required permissions.
2. Pair each group in **Telegram → Group settings** using the dashboard's
   one-time `/shmax_connect CODE` command.
3. In the selected group, map only the required MAX chats to topics.
4. Select that group in **Users** and approve its senders separately.
5. Check delivery to each group and refusal for an unapproved sender.

**Result:** one SHMAX serves several teams. A MAX chat can be mapped to multiple
groups when needed.

**Important boundary:** SHMAX permissions control sending to MAX, not reading
Telegram history. Add and remove members in Telegram. If someone must stop
reading a group, remove their Telegram membership as well as SHMAX send access.

[Group and permission details](multi-group.md).

## 3. An overview for administrators

**Situation:** teams work in separate groups, but a trusted administrator needs
an overview of new conversations and confirmed replies.

**Set it up:**

1. Create a separate private Telegram group for trusted participants only.
2. Connect it to SHMAX and enable **Administrator overview**.
3. Enable **Automatically create topic** if new chats should appear automatically.
4. Check a new incoming MAX message, then a reply from a working group.
   The confirmed reply should appear in the overview.

**Result:** new chats get topics automatically; the overview shows incoming
messages and confirmed replies from working groups. Authorized participants
can also reply from the administrator topic.

**Important:** personal chats from the connected MAX account may appear here.
Choose group members accordingly. Previous MAX history is not imported
automatically. Disabling overview does not remove existing mappings; disable
the group or the relevant routes to stop them completely.

## After setup

- **Delivery stopped:** check connections, the queue and the
  [event journal](event-journal.md). Public reports do not need message content.
- **New teammate:** add them to the correct Telegram group, then approve sending
  in that group through SHMAX. Telegram administrators do not gain it automatically.
- **Update:** keep a verified [backup](backups.md), then run `sudo shmax update`
  or update from the dashboard.
- **Move servers:** use [encrypted export and restore](restore.md).
  Stop the old instance before starting the new one.

See the [FAQ](faq.md) and [release validation report](../../release/v0.2.0/VALIDATION.md)
for external API and platform limits.
