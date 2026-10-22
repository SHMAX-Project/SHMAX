# Independent MAX network (v0.2.0)

Configure MAX networking before phone authentication, or later through
MAX → Network. Choose direct, HTTP CONNECT, SOCKS5 or an Xray subscription.
Save ordinary proxy settings; fetch, select and apply an Xray node. Then check
the saved connection. This tests TCP/TLS without logging in, requesting SMS
or sending messages. Network success is separate from account authentication.

MAX and Telegram have separate encrypted settings, selections and Xray owners.
An unreachable new MAX Xray node restores the previous selection; there is no
implicit direct fallback. Saved MAX sessions reconnect without automatic SMS.
Subscription URLs and proxy passwords are not returned by the API.

Local defaults: Telegram SOCKS 10808, MAX SOCKS 10809, MAX HTTP CONNECT 10810,
all bound to 127.0.0.1. Do not publish them outside the container. Overrides
`SHMAX_XRAY_SOCKS_PORT`, `SHMAX_MAX_XRAY_SOCKS_PORT` and
`SHMAX_MAX_XRAY_HTTP_PORT` must use distinct ports.

MAX SOCKS5 also requires Xray: the local HTTP CONNECT bridge lets both the SDK's
TCP connection and HTTP uploads use the selected SOCKS proxy. SHMAX media
downloads use the same path. Actual MAX availability depends on the node,
not its geographic label.
