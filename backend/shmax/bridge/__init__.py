"""Bridge layer: message mapping and dedup.

* :class:`MessageMapRepository` — persistent MAX ↔ TG id mapping.
* :func:`outbound_marker` — UUID-shaped marker stored on every
  outbound delivery so we can recognise our own messages when
  they bounce back.
"""
