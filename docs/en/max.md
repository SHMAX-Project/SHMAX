# MAX: login, OTP, 2FA

SHMAX uses an ordinary MAX account. Flow: phone → SMS code (may be
slow; do not spam resend) → if 2FA is required, enable it in the
official MAX app first → enter the MAX 2FA password when asked.
In v0.1.2 one MAX recovery manager reconnects the saved session with bounded timeouts, jittered backoff and one listener per connection generation. It cannot request SMS or log out automatically. RECONNECTING / VERIFICATION_UNAVAILABLE indicate recovery or an unavailable check; AUTH_REQUIRED requires explicit operator login when saved authentication is missing or rejected. The dashboard/settings show state and last successful connection; use the [event journal](event-journal.md) for diagnosis.
