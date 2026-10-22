"""Short visible source labels; never invent a human identity or expose phones."""
import re


def safe_label(value, limit=96):
    text=' '.join(str(value or '').split())
    text=''.join(c for c in text if c.isprintable() and c not in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
    text=re.sub(r'\[|\]', '', text)
    return text[:limit].strip()


def source_label(chat_title, sender_name, sender_id, *, show_chat=True, show_sender=True):
    parts=[]
    if show_chat:parts.append(safe_label(chat_title) or 'Чат MAX')
    if show_sender:parts.append(safe_label(sender_name) or ('Участник MAX' if sender_id else 'Автор неизвестен'))
    return ' · '.join(parts) or None
