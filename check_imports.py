"""CI gate: server.py and lint.py import the standard library only."""
import re
import sys

names = getattr(sys, "stdlib_module_names", None)
if names is None:
    sys.exit(0)
src = open("server.py").read() + open("lint.py").read()
mods = {m.split(".")[0] for m in re.findall(r"^(?:import|from) ([\w.]+)", src, re.M)}
bad = sorted(m for m in mods if m not in names and m != "lint")
if bad:
    sys.exit(f"non-stdlib imports: {bad}")
print("stdlib only")
