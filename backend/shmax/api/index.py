"""Landing page (``/``) — minimal HTML.

Kept for the Stage 0 contract. The real Russian Web UI is served
by FastAPI's StaticFiles mount under ``/app/`` in Stage 2 once the
React build artifact is included in the container.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .. import __service_name__, __version__

router = APIRouter()

INDEX_HTML = """<!doctype html>
<html lang=\"ru\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>{service} — {version}</title>
<style>
  :root {{ color-scheme: dark light; }}
  html, body {{
    margin: 0; padding: 0; height: 100%;
    font-family: ui-sans-serif, system-ui, -apple-system, \"Segoe UI\", Roboto,
                 \"Helvetica Neue\", Arial, sans-serif;
    background: #0e1116; color: #e6edf3;
  }}
  main {{
    min-height: 100%; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 1.25rem; padding: 2rem; text-align: center;
  }}
  h1 {{ font-size: clamp(2.5rem, 8vw, 5rem); margin: 0; letter-spacing: -0.02em; }}
  .duck {{ font-size: clamp(4rem, 12vw, 8rem); line-height: 1; }}
  .status {{
    font-size: clamp(1.25rem, 3vw, 1.75rem); color: #58a6ff;
    text-transform: uppercase; letter-spacing: 0.12em;
  }}
  .tagline {{
    font-size: clamp(1rem, 2.4vw, 1.25rem); color: #8b949e;
    max-width: 32rem;
  }}
  .version {{
    margin-top: 1.5rem; font-size: 0.85rem; color: #6e7681;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }}
  code {{ color: #f78166; }}
</style>
</head>
<body>
<main>
  <div class=\"duck\" aria-hidden=\"true\">🦆</div>
  <h1>{service}</h1>
  <div class=\"status\">SHMAX READY</div>
  <div class=\"tagline\">Шмакс жив.</div>
  <div class=\"version\">v{version}</div>
</main>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, summary="SHMAX READY landing page")
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.format(service=__service_name__, version=__version__))
