"""Host helper protocol.

``shmax-hostd`` is a small privileged daemon that runs on the
host (not inside the SHMAX container). The web application
talks to it over a Unix socket using the JSON-line protocol
in :mod:`shmax.host.protocol`. The helper exposes a tiny,
restricted vocabulary (``status``, ``restart``, ``update``,
``rollback``, ``backup``, ``version``); arbitrary shell is
not in the contract.

The web backend never gets a Docker socket.
"""
