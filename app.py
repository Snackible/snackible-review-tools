"""Vercel entrypoint. Vercel looks for a Flask object called `app` in app.py.

Set the environment variable APP=portal on a second Vercel project to serve the review portal instead
of the synthetic dataset dashboard (default).
"""
import os

if os.environ.get("APP") == "portal":
    from portal import app
else:
    from synth_app import app

__all__ = ["app"]
