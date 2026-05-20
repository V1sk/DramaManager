"""Pytest bootstrap.

Runs before any test module is collected, so test files that import the app
at module top level (e.g. `from app.publish import ...`) trigger
`config.load_settings()` with the required env already in place.

admin-accounts-auth made `SESSION_SECRET_KEY` mandatory and `init_db()` now
bootstraps an `admin` account from `ADMIN_INITIAL_PASSWORD`. `setdefault`
leaves any value provided by the surrounding shell untouched.
"""
import os

os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "test-admin-pw")
