# Support / Поддержка

## Before asking / Перед вопросом

1. Open or refresh **Диагностика** to load current diagnostics; use the Telegram settings connection check if needed.
2. Check `docs/ru/troubleshooting.md` (symptom-first).
3. Collect a sanitized report (template below).

## How to ask / Как задать вопрос

Open a GitHub issue with the template below. The more precise the
report, the faster the answer. Russian and English are both fine.

## Sanitized support report template

```
SHMAX version:      <output of `shmax version` or UI footer>
Platform:           <Debian 13 / Armbian; arch amd64/arm64/armv7>
Install mode:       <fresh install / upgrade / repair>
Health:             <MAX: … / Telegram: … / Bridge: …>
Recent log lines:   <docker logs shmax-web-1 --tail 50 (redacted)>
Steps to reproduce: <exact steps>
Expected vs actual: <what should happen / what happens>
```

## DO NOT include / Никогда не прикладывайте

- Telegram API Hash
- Telegram bot token (полностью или частично)
- MAX login/phone/password/2FA secrets
- Xray subscription URL, VLESS UUID, Trojan password
- session files (`/var/lib/shmax/sessions/**`)
- SecretStore key material (`/var/lib/shmax/secrets/**`)
- full unredacted logs

Logs may contain identifiers — always redact anything that looks
like a token, UUID or phone number.
