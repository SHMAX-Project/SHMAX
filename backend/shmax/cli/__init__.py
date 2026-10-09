"""Command-line entry point for SHMAX.

Usage::

    python -m shmax.cli status
    python -m shmax.cli doctor
    python -m shmax.cli logs --tail 100
    python -m shmax.cli setup-link
    python -m shmax.cli backup
    python -m shmax.cli update check
    python -m shmax.cli rollback
    python -m shmax.cli version

Host-level operations that need the host helper daemon return
``host helper unavailable`` rather than crashing on a macOS
dev box.
"""
