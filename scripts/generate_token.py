#!/usr/bin/env python3
"""
Manage API tokens for freeholdy.

Usage:
  python scripts/generate_token.py generate --name "my_laptop"
  python scripts/generate_token.py generate --name "gitlab-ci" --role guest --project myapp
  python scripts/generate_token.py generate --name "ci" --role guest --project a --project b
  python scripts/generate_token.py list
  python scripts/generate_token.py revoke --id 2

Roles: `admin` (default) has full API access. `guest` is scoped to one or more existing
projects and may only redeploy (from each project's own stored git origin), restart, read
logs/status, manage env, list versions and roll back — see app/auth.py. Repeat --project to
scope a guest token to several.
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
from app.models.orm import Project, Token


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cmd_generate(name: str, role: str = "admin", projects: list = None) -> None:
    init_db()
    projects = list(projects or [])
    if role == "guest" and not projects:
        print("❌  A guest token must be scoped to a project: pass --project NAME "
              "(repeat it for several).")
        sys.exit(1)
    if role == "admin" and projects:
        print("❌  An admin token cannot be scoped to projects — drop --project.")
        sys.exit(1)

    token = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        rows = []
        for project in projects:
            row = db.query(Project).filter(Project.name == project).first()
            if not row:
                print(f"❌  Project '{project}' not found — deploy it first, then scope a "
                      "guest token to it.")
                sys.exit(1)
            rows.append(row)
        record = Token(name=name, token_hash=_hash(token), role=role)
        record.projects = rows
        db.add(record)
        db.commit()
    finally:
        db.close()

    scope = f"{role} token" + (f" for {', '.join(projects)}" if projects else "")
    print(f"\n✅  {scope.capitalize()} created for '{name}'")
    print(f"\n    {token}\n")
    print("⚠️   Save this token — it will NOT be shown again.\n")


def cmd_list() -> None:
    init_db()
    db = SessionLocal()
    try:
        # Flatten inside the session — the rows are detached once it closes, so a lazy
        # `token.projects` load would blow up in the print loop below.
        tokens = [
            (t.id, t.name, t.role, ",".join(sorted(p.name for p in t.projects)) or "-",
             t.active, t.created_at)
            for t in db.query(Token).order_by(Token.id).all()
        ]
    finally:
        db.close()

    if not tokens:
        print("No tokens found.")
        return

    # ID first, Name second: update.sh resolves the token it just minted with
    # `awk '$2 == name { print $1 }'` — new columns go after Name, never before.
    print(f"\n{'ID':<5}  {'Name':<24}  {'Role':<7}  {'Projects':<28}  {'Active':<8}  Created at")
    print("─" * 104)
    for tid, name, role, project, active, created_at in tokens:
        print(f"{tid:<5}  {name:<24}  {role:<7}  {project:<28}  "
              f"{'yes' if active else 'no':<8}  {created_at}")
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
    p_gen.add_argument("--role", choices=["admin", "guest"], default="admin",
                       help="admin = full access (default); guest = one project only")
    p_gen.add_argument("--project", action="append", dest="projects", metavar="NAME",
                       help="Project a guest token may act on (required for --role guest; "
                            "repeat for several)")

    sub.add_parser("list", help="List all tokens")

    p_rev = sub.add_parser("revoke", help="Revoke a token by ID")
    p_rev.add_argument("--id", type=int, required=True, help="Token ID to revoke")

    args = parser.parse_args()

    if args.cmd == "generate":
        cmd_generate(args.name, args.role, args.projects)
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "revoke":
        cmd_revoke(args.id)
