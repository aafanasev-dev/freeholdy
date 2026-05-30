#!/usr/bin/env python3
"""
Manage API tokens for freeholdy.

Usage:
  python scripts/generate_token.py generate --name "my_laptop"
  python scripts/generate_token.py list
  python scripts/generate_token.py revoke --id 2
"""

import sys
import os
import secrets
import hashlib
import argparse

# Run from the repo root so relative paths (DATA_DIR, .env) resolve to the
# same files the server uses, regardless of the caller's cwd.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
os.chdir(_REPO_ROOT)

from app.models.database import SessionLocal, init_db
from app.models.orm import Token


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cmd_generate(name: str) -> None:
    init_db()
    token = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        db.add(Token(name=name, token_hash=_hash(token)))
        db.commit()
    finally:
        db.close()

    print(f"\n✅  Token created for '{name}'")
    print(f"\n    {token}\n")
    print("⚠️   Save this token — it will NOT be shown again.\n")


def cmd_list() -> None:
    init_db()
    db = SessionLocal()
    try:
        tokens = db.query(Token).order_by(Token.id).all()
    finally:
        db.close()

    if not tokens:
        print("No tokens found.")
        return

    print(f"\n{'ID':<5}  {'Name':<24}  {'Active':<8}  Created at")
    print("─" * 60)
    for t in tokens:
        print(f"{t.id:<5}  {t.name:<24}  {'yes' if t.active else 'no':<8}  {t.created_at}")
    print()


def cmd_revoke(token_id: int) -> None:
    init_db()
    db = SessionLocal()
    try:
        token = db.query(Token).filter(Token.id == token_id).first()
        if not token:
            print(f"❌  Token ID {token_id} not found.")
            return
        token.active = False
        db.commit()
        print(f"🚫  Token '{token.name}' (ID {token_id}) revoked.")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="freeholdy token manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate", help="Generate a new API token")
    p_gen.add_argument("--name", required=True, help="Label for this token (e.g. 'my_laptop')")

    sub.add_parser("list", help="List all tokens")

    p_rev = sub.add_parser("revoke", help="Revoke a token by ID")
    p_rev.add_argument("--id", type=int, required=True, help="Token ID to revoke")

    args = parser.parse_args()

    if args.cmd == "generate":
        cmd_generate(args.name)
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "revoke":
        cmd_revoke(args.id)
