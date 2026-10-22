# Telegram network: Direct / SOCKS5 / HTTP / Xray

The Telegram Network step controls how the container reaches Bot API. The selected
mode persists across restarts; proxy failures never fall back to Direct.

- **Direct**: Bot API is directly reachable.
- **SOCKS5 / HTTP**: external proxy, host/port and optional credentials.
- **Xray subscription**: VLESS/Trojan subscription URL. SHMAX installs and runs
  local Xray for Telegram only; its SOCKS listener binds to 127.0.0.1.

The first bot `getMe` uses the saved network without starting polling. Fix
connectivity before retrying token verification. Open bot settings from Telegram
and the Telegram Network wizard step to change modes, then check the connection.
MAX has an independent direct, HTTP, SOCKS5 or Xray connection; see [MAX networking](max-network.md).

Optional full member sync requires Telegram MTProto access. Its auxiliary client
uses saved SOCKS/HTTP/Xray or MTProxy settings; without a proxy it connects directly.
Bot API access through HTTP alone does not guarantee MTProto reachability.
Proxy failures do not switch to Direct; sync failures do not stop the normal bridge.
See [member synchronization](telegram-group.md#automatic-participant-discovery).
