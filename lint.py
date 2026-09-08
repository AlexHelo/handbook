"""Drift check for a handbook vault: broken references, banned words, task lines off format.

Run: python3 lint.py --vault /path/to/vault   (exit 1 when anything is found)
Config lives in <vault>/handbook.json under "lint":
  {"files": ["90-Meta/*.md", "~/.claude/skills/*/SKILL.md"],
   "banned": ["second brain"], "sections": ["WUF", "Personal"], "title_max": 60}
"""
import glob
import json
import os
import pathlib
import re
import sys

WIKI = re.compile(r"\[\[([^\]|#]+)")
TICK = re.compile(r"`([^`\n]+)`")
PATHISH = re.compile(r"^(~/|/|[\w.-]+/)[\w./ -]+\.(md|py|json|sh|html|plist)$")
STAMP = re.compile(r"\b\d{1,2}-\d{1,2}\b")


def pages(vault):
    out = set()
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        for f in files:
            if f.endswith(".md"):
                out.add(f[:-3])
    return out


def expand(vault, patterns):
    out = []
    for pat in patterns:
        pat = os.path.expanduser(pat)
        if not os.path.isabs(pat):
            pat = str(vault / pat)
        out += sorted(glob.glob(pat))
    return out


def check_file(path, vault, index, banned):
    found = []
    text = pathlib.Path(path).read_text(errors="replace")
    for n, line in enumerate(text.splitlines(), 1):
        prose = re.sub(r"`[^`]*`", "", line)
        for target in WIKI.findall(prose):
            if target.strip() not in index:
                found.append(f"{path}:{n}: broken link [[{target.strip()}]]")
        for ref in TICK.findall(line):
            if not PATHISH.match(ref) or any(x in ref for x in ("YYYY", "<", "{", "*")):
                continue
            p = pathlib.Path(os.path.expanduser(ref))
            here = pathlib.Path(path).parent
            if not p.is_absolute() and not (vault / p).exists() and not (here / p).exists():
                found.append(f"{path}:{n}: missing path `{ref}`")
            elif p.is_absolute() and not p.exists():
                found.append(f"{path}:{n}: missing path `{ref}`")
        low = line.lower()
        explains = any(x in low for x in ("→", "retired", "old name", "never", "not "))
        for word in banned:
            if not explains and re.search(rf"\b{re.escape(word.lower())}\b", low):
                found.append(f"{path}:{n}: banned word \"{word}\"")
    return found


def check_todo(todo, sections, title_max):
    found = []
    if not todo.exists():
        return found
    section = ""
    for n, line in enumerate(todo.read_text().splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
            if sections and section not in sections and section not in ("Clocks", "Done"):
                found.append(f"{todo}:{n}: section \"{section}\" is not a canonical group")
            continue
        if not line.startswith("- [ ]") or section in ("", "Clocks", "Done"):
            continue
        body = line[5:].strip()
        m = re.match(r"^\*\*(.+?)\*\*", body)
        if not m:
            found.append(f"{todo}:{n}: title is not bold")
            continue
        title = m.group(1)
        if len(title) > title_max:
            found.append(f"{todo}:{n}: title is {len(title)} chars, limit {title_max}")
        if " · " not in body or not STAMP.search(body.rsplit(" · ", 1)[-1]):
            found.append(f"{todo}:{n}: no provenance stamp (· added MM-DD or · from X, MM-DD)")
        if sections and section in sections and title.lower().startswith(section.lower() + " · "):
            found.append(f"{todo}:{n}: title repeats its group tag \"{section} · \"")
    return found


def check_questions(path):
    found, ids = [], set()
    if not path.exists():
        return found
    text = path.read_text()
    for m in re.finditer(r"^## (Q-\d+)[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S):
        n = text[:m.start()].count("\n") + 1
        if m.group(1) in ids:
            found.append(f"{path}:{n}: duplicate id {m.group(1)}")
        ids.add(m.group(1))
        if "**A:**" not in m.group(2):
            found.append(f"{path}:{n}: {m.group(1)} has no **A:** slot")
    return found


def check_memory(path, picks_max=5, bench_max=3):
    found = []
    if not path.exists():
        return found
    text = path.read_text()
    i = text.find("## Tier-1 carry")
    if i == -1:
        return found
    block = text[i:]
    end = block.find("\n## ", 4)
    block = block[:end] if end != -1 else block
    b = block.find("### Bench")
    carry, bench = (block[:b], block[b:]) if b != -1 else (block, "")
    picks = [l for l in carry.splitlines() if l.startswith("- ")]
    on_bench = [l for l in bench.splitlines() if re.match(r"^\d+\.\s", l)]
    if len(picks) > picks_max:
        found.append(f"{path}: {len(picks)} tier-1 picks, limit {picks_max}")
    if len(on_bench) > bench_max:
        found.append(f"{path}: {len(on_bench)} on the bench, limit {bench_max}")
    return found


def run(vault, cfg):
    vault = pathlib.Path(vault).expanduser().resolve()
    index = pages(vault)
    found = []
    for path in expand(vault, cfg.get("files", [])):
        found += check_file(path, vault, index, cfg.get("banned", []))
    found += check_todo(vault / cfg.get("todo", "Todo.md"), cfg.get("sections", []),
                        cfg.get("title_max", 60))
    meta = vault / cfg.get("meta", "90-Meta")
    found += check_questions(meta / "Questions.md")
    found += check_memory(meta / "Assistant-Memory.md", cfg.get("picks_max", 5),
                          cfg.get("bench_max", 3))
    return found


def load(vault):
    p = pathlib.Path(vault).expanduser() / "handbook.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text()).get("lint", {})


if __name__ == "__main__":
    vault = sys.argv[sys.argv.index("--vault") + 1] if "--vault" in sys.argv else "."
    out = run(vault, load(vault))
    print("\n".join(out) if out else "clean")
    sys.exit(1 if out else 0)
