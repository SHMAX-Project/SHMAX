"""Persistent delivery queue.

The queue is the durable outbox of the bridge. Every message
the Core decides to deliver is enqueued first; the worker
picks it up and drives it through ``pending -> processing ->
delivered / retry / failed``.

State transitions are explicit; the DB has a single
``status`` column plus ``attempts``, ``next_attempt_at`` and
``last_error`` for retry policy.
"""
