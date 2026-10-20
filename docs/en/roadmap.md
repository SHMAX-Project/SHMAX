# SHMAX roadmap

## From a bridge to a message router

The next direction is unified routing across accounts, groups and teams.
Administrators will define where messages originate, where they go, and who may
read and reply through each route.

| Direction | Planned behavior | Status |
|---|---|---|
| Multiple accounts | Multiple MAX accounts in one instance, with separate connections and health states | Planned |
| Message router | Explicit routes between account chats, Telegram groups and topics, managed in the web dashboard | Planned |
| Route permissions | User access to specific groups and routes across accounts, with separate send permissions | Planned |
| MAX networking | MAX proxy settings independent of Telegram, with connectivity checks including servers outside Russia | Planned |

The goal is one dashboard for multiple workspaces while keeping conversations
and team permissions separate.

These are planned features, not v0.1.2 capabilities. The current release uses one
MAX account and one linked Telegram group; MAX connects directly. Future proxy
compatibility and MAX availability in each environment need verification.
Dates will follow design and testing.

Share use cases through [GitHub Issues](https://github.com/SHMAX-Project/SHMAX/issues).
