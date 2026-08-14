"""
Uvicorn entrypoint for PIIPS.

The FastAPI application lives in app.py. This module simply re-exports it
so the app can be launched as either `uvicorn app:app` or `uvicorn main:app`.
"""

from app import app

__all__ = ["app"]
