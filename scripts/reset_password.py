"""Reset a user's password from the command line.

Usage:
    python scripts/reset_password.py <username>

In a docker-compose deployment:
    docker compose exec app python /app/scripts/reset_password.py <username>

Reads DATABASE_URL from the environment. Prompts for the new password twice
(no echo), writes a fresh argon2 hash, and deletes the user's existing
sessions so old cookies stop working.
"""
import getpass
import os
import sys

import psycopg
from argon2 import PasswordHasher


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: reset_password.py <username>", file=sys.stderr)
        sys.exit(2)
    username = sys.argv[1].strip()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)

    pw = getpass.getpass("new password: ")
    if getpass.getpass("confirm: ") != pw:
        print("passwords do not match", file=sys.stderr)
        sys.exit(1)
    if not 8 <= len(pw) <= 128:
        print("password must be 8-128 characters", file=sys.stderr)
        sys.exit(1)

    password_hash = PasswordHasher().hash(pw)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE username = %s RETURNING id",
                (password_hash, username),
            )
            row = cur.fetchone()
            if row is None:
                print(f"no user named {username!r}", file=sys.stderr)
                sys.exit(1)
            user_id = row[0]
            cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            killed = cur.rowcount

    print(f"reset password for {username} (revoked {killed} session(s))")


if __name__ == "__main__":
    main()
