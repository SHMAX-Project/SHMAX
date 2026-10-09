# MAX: login, OTP, 2FA

SHMAX uses an ordinary MAX account. Flow: phone → SMS code (may be
slow; do not spam resend) → if 2FA is required, enable it in the
official MAX app first → enter the MAX 2FA password when asked.
Session state is visible on Diagnostics; the watchdog reconnects
automatically after network failures.
