"""Routing engine.

V1 routing model: one MAX chat <-> one Telegram forum topic.

Identification is **strictly by id**: title and display name
are display-only. Two MAX chats with the same title still
belong to different routes.
"""
