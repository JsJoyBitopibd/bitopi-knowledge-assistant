"""A stand-in for the ERP team's verify service (docs/ERP_SIGN_IN_API.md), for tests and a trial before theirs runs.

It follows the contract exactly — POST JSON {username, password} with `Authorization: Bearer <key>`, answer 200
{"valid": true, "user_code", "user_name", "name", …} or {"valid": false} — but checks TEST accounts from a YAML
file with bcrypt hashes, never the ERP's. It speaks plain HTTP, so the assistant must be told
ERP_VERIFY_ALLOW_HTTP=true, which is only acceptable on the same machine.

    python scripts/mock_erp_verify.py --users config/erp_mock_users.example.yaml --port 8601

The key is read from ERP_VERIFY_API_KEY. Requests are logged without their body.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml


def load_accounts(path: Path) -> dict[str, dict[str, Any]]:
    """{user name lower-cased: account}; user names compare without regard to case, as the ERP's do."""
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("users") or {}
    return {str(k).strip().lower(): {**(v or {}), "user_name": str(k).strip()} for k, v in raw.items()}


def verify(accounts: dict[str, dict[str, Any]], username: Any, password: Any) -> dict[str, Any]:
    import bcrypt
    if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
        return {"valid": False}
    a = accounts.get(username.strip().lower())
    if not a or not a.get("active", True) or not bcrypt.checkpw(password.encode("utf-8"),
                                                               str(a.get("password_hash", "")).encode("utf-8")):
        return {"valid": False}                       # wrong password, unknown or inactive: one answer
    out = {"valid": True, "user_code": str(a.get("user_code") or ""), "user_name": a["user_name"]}
    for k in ("name", "employee_code", "company_id", "unit_id", "department"):
        if a.get(k) is not None:
            out[k] = str(a[k])
    return out


def make_server(accounts: dict[str, dict[str, Any]], key: str, host: str = "127.0.0.1", port: int = 0,
                path: str = "/verify") -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:          # noqa: N802 (http.server's name)
            if self.path != path:
                return self._send(404, {"error": "not found"})
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode("utf-8"), f"Bearer {key}".encode("utf-8")):
                return self._send(401, {"error": "unauthorized"})
            try:
                n = min(int(self.headers.get("Content-Length", "0")), 65536)
                req = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": "bad request"})
            if not isinstance(req, dict):
                return self._send(400, {"error": "bad request"})
            self._send(200, verify(accounts, req.get("username"), req.get("password")))

        def log_message(self, fmt: str, *args: Any) -> None:   # the request line only, never the body
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=Path, default=Path("config/erp_mock_users.example.yaml"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8601)
    args = ap.parse_args()
    key = os.environ.get("ERP_VERIFY_API_KEY", "")
    if not key:
        sys.exit("ERP_VERIFY_API_KEY is not set")
    server = make_server(load_accounts(args.users), key, args.host, args.port)
    print(f"mock ERP verify service on http://{args.host}:{args.port}/verify ({args.users})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
