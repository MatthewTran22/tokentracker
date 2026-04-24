"""Vercel entry point.

Vercel's Python runtime looks for functions under /api. We re-export the
FastAPI app so a single serverless function handles every route — avoids
one cold start per endpoint and one DB pool per endpoint.
"""

import sys
from pathlib import Path

# Make the src/ layout importable without `pip install -e .` on Vercel.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tokentracker.main import app  # noqa: E402

# Vercel's Python runtime expects `app` at module level for ASGI.
__all__ = ["app"]
