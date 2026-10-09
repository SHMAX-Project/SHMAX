# Xray: subscription, nodes, VLESS/Trojan

Subscription URL is stored encrypted (SecretStore); body limit
2 MiB; supported: VLESS/Trojan over tcp/ws/grpc, TLS/Reality,
Vision on TCP only (gRPC+Reality+Vision rejected — semantic
validation plus a real `xray run -test`). SHMAX runs exactly one
Xray process with an ephemeral 0600 config, waits for the loopback
SOCKS to accept, and points Telegram at it. The selected node is
cached: restart works without the subscription server. Any
cancellation/timeout still reaps the child (TERM → KILL → confirmed
reap). Xray binary is pinned (v25.3.6, SHA256 checked).
