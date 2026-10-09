"""Telegram adapter layer.

V1 supports two transports:

* :class:`TelegramBotApiAdapter` (aiogram, HTTP Bot API)
* :class:`TelegramMtProtoAdapter` (Telethon, MTProto)

Both implement the same :class:`TelegramAdapter` ABC defined
in Stage 0. The Core depends only on the ABC.

A :class:`TelegramTransportManager` selects which concrete
adapter to instantiate and provides configuration for direct /
SOCKS / HTTP / MTProxy transports.
"""
