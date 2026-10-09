"""Production entry point: ``uvicorn shmax.main:app``."""

import os

from .api.app import create_app
from .logging_setup import configure_logging

# Configure root logging BEFORE the app (and its runtime) starts so
# operators see listener/xray lifecycle events under uvicorn.
configure_logging(os.environ.get("SHMAX_LOG_LEVEL"))

# ``app`` is the default-configured FastAPI instance. Tests
# build isolated instances via ``create_app(settings=...)``.
app = create_app()
