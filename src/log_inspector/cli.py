"""Admin commands: `uv run log-inspector <command>`.

Users are created here rather than through the API, so there is no unauthenticated sign-up.
"""

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from log_inspector.config import Settings
from log_inspector.db import create_db_engine, init_db
from log_inspector.models import ApiKey, User
from log_inspector.services import auth


def _print_key(plaintext: str) -> None:
    print()
    print(f"  API key: {plaintext}")
    print()
    print("Store it now: it can't be shown again. Send it as 'Authorization: Bearer <key>'.")


def create_user(db: Session, args: argparse.Namespace) -> int:
    user = auth.create_user(db, args.name)
    _, plaintext = auth.create_api_key(db, user, args.key_name)
    print(f"Created user '{user.name}' (id {user.id}).")
    if args.claim_unowned:
        claimed = auth.claim_unowned_connections(db, user)
        print(f"Assigned {claimed} existing connection(s) to this user.")
    _print_key(plaintext)
    return 0


def create_key(db: Session, args: argparse.Namespace) -> int:
    user = db.get(User, args.user_id)
    if user is None:
        print(f"No user with id {args.user_id}. See `log-inspector list-users`.", file=sys.stderr)
        return 1
    _, plaintext = auth.create_api_key(db, user, args.name)
    print(f"Created key '{args.name}' for user '{user.name}'.")
    _print_key(plaintext)
    return 0


def list_users(db: Session, args: argparse.Namespace) -> int:
    for user in db.scalars(select(User).order_by(User.created_at)):
        print(f"{user.id}  {user.name}")
        for key in user.api_keys:
            state = "revoked" if key.revoked_at else "active"
            print(f"    key {key.id}  li_{key.prefix}_…  {key.name}  ({state})")
    return 0


def revoke_key(db: Session, args: argparse.Namespace) -> int:
    key = db.get(ApiKey, args.key_id)
    if key is None:
        print(f"No API key with id {args.key_id}.", file=sys.stderr)
        return 1
    auth.revoke_api_key(db, key.user, key.id)
    print(f"Revoked key '{key.name}' (li_{key.prefix}_…).")
    return 0


def migrate(db: Session, args: argparse.Namespace) -> int:
    # main() has already run the migrations; this command exists to run them on their own,
    # e.g. as a deploy step before starting the server.
    print("Database schema is up to date.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="log-inspector", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("create-user", help="Create a user and their first API key.")
    p.add_argument("name")
    p.add_argument("--key-name", default="default")
    p.add_argument(
        "--claim-unowned",
        action="store_true",
        help="Give this user the connections created before users existed.",
    )
    p.set_defaults(handler=create_user)

    p = commands.add_parser("create-key", help="Create another API key for a user.")
    p.add_argument("user_id")
    p.add_argument("--name", default="default")
    p.set_defaults(handler=create_key)

    p = commands.add_parser("list-users", help="List users and their API keys.")
    p.set_defaults(handler=list_users)

    p = commands.add_parser("revoke-key", help="Revoke an API key by its id.")
    p.add_argument("key_id")
    p.set_defaults(handler=revoke_key)

    p = commands.add_parser("migrate", help="Upgrade the database schema, then exit.")
    p.set_defaults(handler=migrate)

    args = parser.parse_args(argv)
    engine = create_db_engine(Settings().database_url)
    init_db(engine)
    with Session(engine) as db:
        return args.handler(db, args)


if __name__ == "__main__":
    sys.exit(main())
