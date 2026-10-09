"""Validation of SHMAX-local Telegram → MAX attribution overrides."""
import unicodedata
from typing import Optional


def normalize_nickname(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    # Reject line/control/bidi injection, while retaining emoji joiners.
    if any(unicodedata.category(c) in {'Cc', 'Zl', 'Zp'} or
           (unicodedata.category(c) == 'Cf' and c not in '\u200c\u200d')
           for c in value):
        raise ValueError('Подпись не должна содержать переносы строк или управляющие символы.')
    value = value.strip()
    if len(value) > 64:
        raise ValueError('Подпись должна содержать не более 64 символов.')
    return value or None
