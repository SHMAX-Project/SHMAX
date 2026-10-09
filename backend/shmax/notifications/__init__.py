"""Notifications dispatcher.

Forwards watchdog events to a Telegram system topic. Falls
back to the logging dispatcher when no Telegram is configured
or when Telegram itself is DOWN (we queue locally and replay
when the operator's bot recovers).
"""

from .dispatcher import (
    BufferingNotificationDispatcher,
    TelegramNotificationDispatcher,
    build_notification_dispatcher,
)
from .outbox import NotificationOutbox
from .outbox_dispatcher import OutboxBackedDispatcher
from .sender import NotificationSender
from .system_topic import SystemTopicService, SystemTopicRecord, SYSTEM_TOPIC_TITLE

__all__ = [
    "BufferingNotificationDispatcher",
    "NotificationOutbox",
    "NotificationSender",
    "OutboxBackedDispatcher",
    "SystemTopicRecord",
    "SystemTopicService",
    "SYSTEM_TOPIC_TITLE",
    "TelegramNotificationDispatcher",
    "build_notification_dispatcher",
]
