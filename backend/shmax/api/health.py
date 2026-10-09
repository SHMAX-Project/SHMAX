"""/health endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __service_name__, __version__

router = APIRouter()


@router.get("/health", summary="Liveness + readiness snapshot")
def health(request: Request) -> dict:
    """Return basic liveness plus a snapshot of persistent paths."""
    layout = request.app.state.paths
    existing = []
    for p in layout.all_persistent():
        rel = str(p.relative_to(layout.root)).replace("\\", "/")
        existing.append(rel)
    return {
        "status": "ok",
        "service": __service_name__,
        "version": __version__,
        "persistent": {
            "root": str(layout.root),
            "writable": True,
            "existing": existing,
            "missing": [],
        },
    }
