"""Stage 3 preview on the server (Phase N2): what the playbooks offer for today's open red and amber Quality
files, and why each option is or is not ready. Read-only — nothing is written, no model is asked.

    python scripts/recommend_preview.py            # every factory
    python scripts/recommend_preview.py --limit 10

The app shows the same preview to administrators only, and only while agents.recommendations.enabled is true.
With sign-in off nobody is an administrator in the app, so this script is how the owner sees it before S3.
"""
import argparse, _path  # noqa: F401

from ragbot.auth.models import Scope
from ragbot.domain_agents import recommend


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()
    everyone = Scope(factories=frozenset(["*"]), is_admin=True)
    opts = recommend.options(everyone, limit=a.limit)
    if not opts:
        print("no option: no open red or amber Quality file matches a playbook in config/playbooks/")
        return
    ready = sum(o.recommendable for o in opts)
    print(f"{len(opts)} option(s), {ready} ready to recommend\n")
    for o in opts:
        print(f"{o.level:<5} {o.factory:<4} {o.subject:<28} {o.action_title}  [{o.playbook}, {o.playbook_status}]")
        if o.cost:
            print(f"      cost: {recommend.cost_text(o.cost)}")
        if o.params:
            print(f"      chosen by a person: {recommend.params_summary(o.params)}")
        if o.blocked:
            print(f"      not yet: {'; '.join(o.blocked)}")


if __name__ == "__main__":
    main()
