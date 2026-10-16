"""Stage 4 on the server (Phase O2): write one copy-only draft from cited facts and print it. Nothing is sent.

    python scripts/draft.py supplier_escalation "PO 4500123 from Coats"
    python scripts/draft.py meeting_agenda "lines below target at TAL today"
    python scripts/draft.py qc_investigation "DHU of file FR-26-317"
    python scripts/draft.py --list

The app offers the same to administrators only, while agents.drafts.enabled is true; with sign-in off nobody is
an administrator in the app, so this script is how the owner tries drafts before then. It runs with every factory
in scope, as the owner. Each draft calls the model twice or three times (the facts, the draft, one retry).
"""
import argparse, _path  # noqa: F401

from ragbot.agent import drafts
from ragbot.auth.models import Scope


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", nargs="?", choices=list(drafts.KINDS))
    ap.add_argument("subject", nargs="?", default="")
    ap.add_argument("--list", action="store_true", help="the draft kinds")
    ap.add_argument("--user", default="owner", help="the name the draft is stored under")
    a = ap.parse_args()
    if a.list or not a.kind:
        for k, title in drafts.KINDS.items():
            print(f"{k:<24} {title}")
        return
    d = drafts.make(a.kind, a.subject, user=a.user, scope=Scope(factories=frozenset(["*"]), is_admin=True))
    if not d.ok:
        print("No draft:", "; ".join(d.problems))
        raise SystemExit(3)
    print(d.text)
    print()
    for r in d.references:
        where = f"{r.source} p.{r.page}" if r.kind == "pdf" else f"{r.database} {', '.join(r.views or [])}"
        print(f"[{r.marker}] {where}")
    blanks = drafts.placeholders(d.text)
    if blanks:
        print("\nTo fill in before sending:", ", ".join(blanks))
    print(f"\nstored as draft #{d.id} (data/index/drafts.db); nothing was sent")


if __name__ == "__main__":
    main()
