"""Adapter layer.

The Core of SHMAX never depends on PyMax, aiogram, or Telethon
directly — it depends on these interfaces. Concrete adapters
will be plugged in during the next stages.
"""
