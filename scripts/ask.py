"""Ask one question from the command line. Usage: python scripts/ask.py "question" [--category SOP]"""
import argparse, _path  # noqa: F401
from ragbot.agent.orchestrator import answer
from ragbot.auth.models import Scope
from ragbot.agent.citations import render_references

p = argparse.ArgumentParser()
p.add_argument("question", nargs="+")
p.add_argument("--category", action="append")
a = p.parse_args()
res = answer(" ".join(a.question), where={"category": a.category} if a.category else None, user="cli",
             scope=Scope.unrestricted())   # a command-line operator sees everything
print(res.text)
if res.references:
    print("\nReferences")
    print(render_references(res.references))
print(f"\n[route={res.route} | in={res.usage.input_tokens} out={res.usage.output_tokens} | not_found={res.not_found}]")
for w in res.warnings:
    print("  warning:", w)
