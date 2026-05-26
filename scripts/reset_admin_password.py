"""Emergency: reset the `admin` account's password directly in `hls.db`.

Usage:
    ./venv/bin/python scripts/reset_admin_password.py <new_password>

Sets `must_change_pw=1` so the next login forces a real password (keeping the
emergency password out of operators' shell history). Does NOT touch other
accounts.

Run with the server stopped to avoid WAL surprises. If a different account is
locked out, pass --user <name> instead of admin.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from passlib.context import CryptContext  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="reset an account's password")
    ap.add_argument("new_password", help="new password (>= 6 chars)")
    ap.add_argument("--user", default="admin", help="username (default: admin)")
    ap.add_argument(
        "--db", default=str(REPO / "hls.db"), help="path to hls.db",
    )
    args = ap.parse_args()

    if len(args.new_password) < 6:
        print("ERROR: password must be at least 6 chars", file=sys.stderr)
        return 2

    pwd_hash = CryptContext(schemes=["bcrypt"]).hash(args.new_password)
    con = sqlite3.connect(args.db)
    try:
        cur = con.execute(
            "UPDATE users SET password_hash=?, must_change_pw=1 "
            "WHERE username=?",
            (pwd_hash, args.user),
        )
        con.commit()
    finally:
        con.close()

    if cur.rowcount == 0:
        print(f"ERROR: user {args.user!r} not found in {args.db}", file=sys.stderr)
        return 1
    print(f"OK: reset password for {args.user!r}; must_change_pw=1")
    print("Restart the server, log in, and change the password on first request.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
