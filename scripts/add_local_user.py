"""Add or update a local sign-in user (auth.provider: local — development and tests only; the pilot uses
Active Directory). Asks for the password and stores only its bcrypt hash in config/users.yaml.

    python scripts/add_local_user.py jdoe --groups KA-Staff-TAL [--name "J. Doe"]
"""
import argparse, getpass, sys, _path  # noqa: F401

import bcrypt
import yaml

from ragbot.auth.providers import account_name
from ragbot.config import settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("user"); ap.add_argument("--groups", required=True, help="comma list, from config/scopes.yaml")
    ap.add_argument("--name", default="")
    a = ap.parse_args()
    name = account_name(a.user)
    if not name:
        sys.exit("user names are letters, digits, '.', '_' and '-' only")
    pw = getpass.getpass(f"password for {name}: ")
    if len(pw) < 10 or pw != getpass.getpass("again: "):
        sys.exit("passwords differ or are shorter than 10 characters")
    path = settings().path("users_file", "config/users.yaml")
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    users = data.get("users") or {}
    users[name] = {"name": a.name or name, "groups": [g.strip() for g in a.groups.split(",") if g.strip()],
                   "password_hash": bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("ascii")}
    data["users"] = users
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
    print(f"saved {name} ({', '.join(users[name]['groups'])}) to {path}")


if __name__ == "__main__":
    main()
