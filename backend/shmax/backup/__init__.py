"""Backup manager.

V1 produces an unencrypted ``.shmax`` tar.gz archive containing:

* ``manifest.json``         — version + paths + checksums
* ``db/shmax.db``           — SQLite snapshot
* ``config/``               — JSON / YAML configs (secrets excluded)
* ``secrets/``              — only the encrypted SecretStore file
* ``sessions/``             — PyMax + Telethon session files
* ``state/``                — runtime state blobs
* ``backup_format.txt``     — constant string for forward-compat

The archive does NOT include the ``cache/`` subdirectory. It
does NOT include the ``master.key`` — restore requires the
caller to copy it back into the persistent root separately.

Backup format is plain gzip-tar with a JSON manifest. No
custom encryption: that is the next stage after live
validation confirms the rest of the bridge works.
"""
