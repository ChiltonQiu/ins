"""Create an account, or reset the password on one that exists.

    python -m scripts.add_user anne@agency.com

The password is read from the terminal, never from an argument: argv lands in
shell history and is visible in the process table to every other user on the
machine.

There is no signup route and no reset email. For an agency of a few people,
the person with shell access is the provisioning system, and this is the whole
of it.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from renewal.auth.passwords import hash_password
from renewal.db import get_engine
from renewal.models import User

MIN_LENGTH = 12


def upsert_user(
    session: Session, email: str, password: str, display_name: str
) -> tuple[User, bool]:
    """Returns the account and whether it was created. Resetting also clears a
    lockout and reactivates a deactivated account — a reset is how somebody
    locked out gets back in."""
    if len(password.strip()) < MIN_LENGTH:
        raise ValueError(f"password must be at least {MIN_LENGTH} characters")
    address = email.strip()
    user = session.scalar(
        select(User).where(func.lower(User.email) == address.lower())
    )
    created = user is None
    if user is None:
        user = User(email=address, password_hash="", display_name=display_name)
        session.add(user)
    user.password_hash = hash_password(password)
    user.failed_count = 0
    user.locked_until = None
    user.is_active = True
    session.flush()
    return user, created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument(
        "--name", default=None, help="display name; defaults to the address"
    )
    args = parser.parse_args(argv)

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1

    session = sessionmaker(bind=get_engine())()
    try:
        user, created = upsert_user(
            session, args.email, password, args.name or args.email.strip()
        )
        session.commit()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        session.close()

    print(f"{'Created' if created else 'Password reset for'} {user.email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
