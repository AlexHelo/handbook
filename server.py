import datetime
import hashlib
import html
import json
import os
import pathlib
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = pathlib.Path(__file__).resolve().parent


def _vault_arg():
    if "--vault" in sys.argv:
        return sys.argv[sys.argv.index("--vault") + 1]
    return os.environ.get("HANDBOOK_VAULT") or _HERE / "starter"


_VAULT = pathlib.Path(_vault_arg()).expanduser().resolve()
CONFIG_FILE = _VAULT / "handbook.json"
PATH_KEYS = ("todo", "meta", "questions", "memory", "capture_log", "brief",
             "archive", "attio_token", "linear_key_path")

CONFIG = {
    "owner": "",
    "wordmark": "handbook",
    "accent": "#FF4A00",
    "accent_dark": "#FF5A14",
    "port": 4321,
    "vault": _VAULT,
    "todo": _VAULT / "Todo.md",
    "meta": _VAULT / "90-Meta",
    "questions": _VAULT / "90-Meta" / "Questions.md",
    "memory": _VAULT / "90-Meta" / "Assistant-Memory.md",
    "capture_log": _VAULT / "90-Meta" / "Capture-Log.md",
    "brief": _VAULT / ".dashboard" / "brief.html",
    "archive": _VAULT / "90-Meta" / "Todo-Archive.md",
    "sections": [],
    "integrations": ["attio", "linear", "github"],
    "linear_mine": True,
    "lint": {},
    "attio_token": pathlib.Path.home() / ".attio-token",
    "linear_key_path": pathlib.Path.home() / ".secrets" / "linear-api-key",
    "repos": [],
    "repos_git": [_VAULT],
    "panels": [{"name": "build", "enabled": True, "order": 1},
               {"name": "linear", "enabled": True, "order": 2},
               {"name": "attio", "enabled": True, "order": 3}],
    "icon_projects": {},
    "hide_dirs": {"_sources", "node_modules", ".tmp"},
    "home_page": "00-Home.md",
    "attio_url": "https://app.attio.com",
    "github_url": "https://github.com",
    "linear_url": "https://linear.app",
    "runs": {"brief": "morning-brief", "sync": "capture-sync"},
    "logo": "",
    "handbooks": [],
}


def secret(env, path):
    """An env var wins; otherwise the file. Missing both reads as empty."""
    val = os.environ.get(env)
    if val:
        return val.strip()
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def on(name):
    return name in CONFIG["integrations"]


def save(path, text):
    """Atomic replace so a crash or a racing agent never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


_write_lock = threading.Lock()


def _path(v):
    p = pathlib.Path(str(v)).expanduser()
    return p if p.is_absolute() else _VAULT / p


def load_config(path=None):
    """Overlay handbook.json on the defaults. Paths resolve against the vault."""
    path = path or CONFIG_FILE
    if not path.exists():
        return
    user = json.loads(path.read_text())
    for k, v in user.items():
        if k in PATH_KEYS:
            CONFIG[k] = _path(v)
        elif k == "repos_git":
            CONFIG[k] = [_path(x) for x in v]
        elif k == "hide_dirs":
            CONFIG[k] = set(v)
        else:
            CONFIG[k] = v


load_config()

VAULT = CONFIG["vault"]
TODO = CONFIG["todo"]
META = CONFIG["meta"]
QUESTIONS = CONFIG["questions"]
MEMORY = CONFIG["memory"]
CAPTURE_LOG = CONFIG["capture_log"]
BRIEF = CONFIG["brief"]
ARCHIVE = CONFIG["archive"]
ATTIO_TOKEN = CONFIG["attio_token"]
REPOS = CONFIG["repos"]
PORT = CONFIG["port"]
INBOX_HEADER = "## Inbox from dashboard"
GH_WINDOW_DAYS = 14
AGING_DAYS = 14
RECENT_DAYS = 7
BRIEF_STALE_HOURS = 26
SYNC_STALE_HOURS = 4
SYNC_WINDOW = (12, 22)
BRIEF_AT = (7, 15)
DIFF_MAX_LINES = 3000
LOG_PER_REPO = 15
LOG_MERGED_MAX = 40
BENCH_MAX = 3
NEXT_UP = 5
WATCH = {"todo": "todo", "questions": "questions", "capture": "capture_log",
         "memory": "memory", "brief": "brief"}
GRAPH_LAYOUT_VERSION = 3
SSE_MAX = 4
SSE_TICK = 1.0
SSE_PING = 20
ASSET_DIR = _HERE / "assets"
ASSET_TYPES = {".png": "image/png", ".svg": "image/svg+xml",
               ".webp": "image/webp", ".jpg": "image/jpeg", ".ico": "image/x-icon"}

E = html.escape
_cache = {}
_busy = set()
_locks = {}
_lock = threading.Lock()


def _fill(key, fn):
    try:
        val = fn()
        with _lock:
            _cache[key] = (time.time(), val)
    except Exception:
        pass
    finally:
        with _lock:
            _busy.discard(key)


def cached(key, ttl, fn):
    """Stale-while-revalidate: only the very first call for a key blocks."""
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] >= ttl and key not in _busy:
            _busy.add(key)
            threading.Thread(target=_fill, args=(key, fn), daemon=True).start()
        if hit:
            return hit[1]
        gate = _locks.setdefault(key, threading.Lock())
    with gate:
        with _lock:
            hit = _cache.get(key)
        if hit:
            return hit[1]
        val = fn()
        with _lock:
            _cache[key] = (time.time(), val)
        return val


# ------------------------------------------------------------- live updates

_sse = []
_sse_lock = threading.Lock()


def git_head_stamp():
    """Changes the moment a commit lands, without shelling out to git."""
    g = CONFIG["vault"] / ".git"
    paths = [g / "HEAD"]
    try:
        head = paths[0].read_text().strip()
    except OSError:
        return 0
    if head.startswith("ref:"):
        paths.append(g / head[4:].strip())
    total = 0
    for p in paths:
        try:
            total += p.stat().st_mtime_ns
        except OSError:
            pass
    return total


def watch_stamp():
    out = {}
    for name, key in WATCH.items():
        try:
            out[name] = CONFIG[key].stat().st_mtime_ns
        except OSError:
            out[name] = 0
    out["commits"] = git_head_stamp()
    return out


def sse_add(q):
    """Register a stream, dropping the oldest once the cap is passed."""
    with _sse_lock:
        _sse.append(q)
        while len(_sse) > SSE_MAX:
            old = _sse.pop(0)
            try:
                old.put_nowait(None)
            except queue.Full:
                pass


def sse_drop(q):
    with _sse_lock:
        if q in _sse:
            _sse.remove(q)


def broadcast(source):
    with _sse_lock:
        streams = list(_sse)
    for q in streams:
        try:
            q.put_nowait(source)
        except queue.Full:
            pass


def watcher():
    prev = watch_stamp()
    while True:
        time.sleep(SSE_TICK)
        now = watch_stamp()
        for name, value in now.items():
            if prev.get(name) != value:
                broadcast(name)
        prev = now


# ---------------------------------------------------------------- vault read

def clean(s):
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"~~([^~]+)~~", r"\1", s)
    s = re.sub(r"\*([^*\n]+)\*", r"\1", s)
    return s.replace("`", "").strip()


def task_id(raw):
    return hashlib.md5(raw.strip().encode()).hexdigest()[:12]


CHUNK_RE = re.compile(
    r"\s*·\s*((?:added|from|updated|found|closed|built|sent|shipped|filed|split"
    r"|verified|dated|retargeted|asked|parked|done|flagged|reopened|reframe)\b"
    r"[^·]*)$", re.I)
BOLD_RE = re.compile(r"^\*\*(.+?)\*\*(.*)$")
MMDD_RE = re.compile(r"\b(\d{1,2})-(\d{1,2})\b")

CLOSE_RE = re.compile(r"^closed from dashboard \d{1,2}-\d{1,2}(: not needed)?$", re.I)
REOPEN_RE = re.compile(r"^reopened from dashboard \d{1,2}-\d{1,2}$", re.I)
REFRAME_RE = re.compile(r"^reframe asked via dashboard \d{1,2}-\d{1,2}$", re.I)
UNSURE_RE = re.compile(r"^flagged unsure via dashboard \d{1,2}-\d{1,2}$", re.I)


def line_body(line):
    """The task text of a `- [ ] …` line, the exact string chunk offsets index."""
    return line.rstrip("\n")[5:].strip()


def trailing_chunks(body):
    """Every trailing `· provenance` chunk, as (start, end, text) into body."""
    out, cut = [], body
    while True:
        m = CHUNK_RE.search(cut)
        if not m:
            break
        out.append((m.start(), m.end(), m.group(1).strip()))
        cut = cut[:m.start()]
    out.reverse()
    return out


def split_task(body):
    """Title / description / source from a Todo line body, either format.

    New format is `**Short title** - description · source`; the messy legacy
    lines are the same shape with the title run overgrown or absent."""
    chunks = trailing_chunks(body)
    source = " · ".join(c[2] for c in chunks)
    if chunks:
        body = body[:chunks[0][0]].rstrip(" ·")
    m = BOLD_RE.match(body.strip())
    if m:
        title, rest = clean(m.group(1)), clean(m.group(2))
    else:
        text = clean(body)
        title = text
        for sep in (" — ", " – ", ": ", ". "):
            idx = text.find(sep)
            if 12 <= idx <= 95:
                title = text[:idx]
                break
        rest = text[len(title):]
    rest = rest.lstrip(" —–:.·-")
    if len(title) > 100:
        cut = title.rfind(" ", 0, 100)
        cut = cut if cut > 50 else 100
        rest = (title[cut:].strip() + " " + rest).strip()
        title = title[:cut].rstrip(" .,;:")
    return title.strip(), rest.strip(), clean(source)


def task_state(body):
    """What the dashboard has already done to this line, read back off it."""
    chunks = trailing_chunks(body)
    st = {"flags": [], "closed_here": False, "not_needed": False,
          "reframed": False, "reopened": False}
    for i, (_, _, text) in enumerate(chunks):
        low = text.strip()
        if low.lower().startswith("flagged"):
            st["flags"].append({"n": i, "text": low,
                                "unsure": bool(UNSURE_RE.match(low))})
        elif CLOSE_RE.match(low):
            st["closed_here"] = True
            st["not_needed"] = low.lower().endswith("not needed")
        elif REOPEN_RE.match(low):
            st["reopened"] = True
        elif REFRAME_RE.match(low):
            st["reframed"] = True
    return st


def age_days(source):
    """Days since the freshest MM-DD stamp in a source suffix, or None."""
    today = datetime.date.today()
    dates = []
    for mm, dd in MMDD_RE.findall(source):
        try:
            when = datetime.date(today.year, int(mm), int(dd))
        except ValueError:
            continue
        if (when - today).days > 30:
            when = when.replace(year=today.year - 1)
        dates.append(when)
    return (today - max(dates)).days if dates else None


def todo_sections():
    if not TODO.exists():
        return []
    sections, current, item = [], None, None
    for line in TODO.read_text().splitlines():
        if line.startswith("## "):
            current = {"title": clean(line[3:]), "items": [], "clocks": []}
            sections.append(current)
            item = None
        elif current is None:
            continue
        elif line[:5] in ("- [ ]", "- [x]", "- [X]"):
            body = line_body(line)
            title, rest, source = split_task(body)
            item = {"id": task_id(line), "title": title, "rest": rest,
                    "source": source, "age": age_days(source),
                    "project": current["title"], "done": line[3] in "xX"}
            item.update(task_state(body))
            current["items"].append(item)
        elif line.startswith("  ") and line.strip() and item is not None:
            item["rest"] = (item["rest"] + "\n" + clean(line.strip())).strip()
        elif current["title"].startswith("Clocks") and line.startswith("- "):
            if "resolved from dashboard" not in line:
                current["clocks"].append({"text": clean(line[2:]),
                                          "id": task_id(line)})
            item = None
        elif not line.strip():
            pass
        else:
            item = None
    return sections


def clock_rows(clocks, sections):
    rows = []
    for entry in clocks:
        c = entry["text"]
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", c)
        head = re.split(r"\s[—–-]\s", c, 1)
        project = head[0].strip()
        what = head[1].strip() if len(head) > 1 else ""
        what = re.sub(r"\s*·.*$", "", what)
        what = re.sub(r"\s*\d{4}-\d{2}-\d{2}\s*", " ", what).strip()
        left, due = None, None
        if m:
            due = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            left = (due - datetime.date.today()).days
        token = re.split(r"[/\s]", project)[0].lower()
        opened = sum(1 for s in sections for t in s["items"]
                     if not t["done"] and token in (t["title"] + " " + t["rest"]).lower())
        rows.append({"project": project, "what": what, "left": left,
                     "due": due, "open": opened, "id": entry["id"]})
    return rows


def focus_picks():
    """The morning brief's own tier-1 picks and bench, read back."""
    if not MEMORY.exists():
        return [], []
    text = MEMORY.read_text()
    i = text.find("## Tier-1 carry")
    if i == -1:
        return [], []
    block = text[i:]
    end = block.find("\n## ", 4)
    block = block[:end] if end != -1 else block
    bench_at = block.find("### Bench")
    carry_block = block[:bench_at] if bench_at != -1 else block
    bench_block = block[bench_at:] if bench_at != -1 else ""

    picks = []
    for line in carry_block.splitlines():
        if not line.startswith("- "):
            continue
        raw = line[2:]
        why = ""
        w = re.search(r"\*\(([^)]*)\)\*\s*$", raw)
        if w:
            why = w.group(1).strip()
            raw = raw[:w.start()]
        day = None
        d = re.search(r"[—–-]\s*(\d+)d\s*$", raw.strip())
        if d:
            day = int(d.group(1))
            raw = raw[:d.start()]
        picks.append({"text": clean(raw), "day": day, "why": clean(why)})

    bench = []
    for line in bench_block.splitlines():
        m = re.match(r"^\d+\.\s+(.*)", line)
        if m:
            bench.append(clean(re.sub(r"\*\([^)]*\)\*\s*$", "", m.group(1))))
    return picks, bench


def match_task(text, items):
    """Unique Todo item a focus line refers to, or None. Never guesses."""
    words = {w for w in re.findall(r"[a-z0-9]{4,}", text.lower())}
    if len(words) < 3:
        return None
    need = max(3, int(len(words) * 0.6))
    scored = [(sum(1 for w in words if w in (t["title"] + " " + t["rest"]).lower()), t)
              for t in items]
    hits = [(n, t) for n, t in scored if n >= need]
    if not hits:
        return None
    top = max(n for n, _ in hits)
    best = [t for n, t in hits if n == top]
    return best[0] if len(best) == 1 else None


def open_queue_questions():
    """Unanswered Q-NNN entries under `## Open`, body kept as raw markdown."""
    if not QUESTIONS.exists():
        return []
    text = QUESTIONS.read_text()
    start = text.find("\n## Open")
    if start != -1:
        end = text.find("\n## Answered", start)
        text = text[start:end if end != -1 else len(text)]
    out = []
    for m in re.finditer(r"^## (Q-\d+)[ ·]*(.*)$", text, re.M):
        nxt = text.find("\n## ", m.end())
        block = text[m.end():nxt if nxt != -1 else len(text)].split("**A:**", 1)
        if len(block) > 1 and block[1].strip(" \t\n-"):
            continue
        body = block[0].strip()
        head, asked = m.group(2).strip(), ""
        a = re.search(r"·\s*asked\s*([\d-]+)\s*$", head)
        if a:
            asked = "asked " + a.group(1)
            head = head[:a.start()].strip(" ·")
        first = next((clean(l) for l in body.splitlines() if l.strip()), "")
        out.append({"id": m.group(1), "title": clean(head),
                    "asked": asked, "body": body, "first": first})
    return out


def inbox_entries():
    if not QUESTIONS.exists():
        return []
    text = QUESTIONS.read_text()
    idx = text.find(INBOX_HEADER)
    if idx == -1:
        return []
    return [clean(l[2:]) for l in text[idx:].splitlines() if l.startswith("- ")]


def intake_files():
    """Intake forms still taking answers. `status: active` in the frontmatter."""
    out = []
    for p in sorted(META.glob("Founder-Intake-*.md"), reverse=True):
        try:
            if frontmatter(p.read_text())[0].get("status") == "active":
                out.append(p)
        except OSError:
            continue
    return out


def intake_questions(path):
    text = path.read_text()
    out, section = [], ""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("## "):
            section = clean(line[3:])
            continue
        m = re.match(r"^(\d+)\.\s+(.*)", line)
        if not m:
            continue
        answered = any(l.startswith("> **A (") for l in lines[idx + 1:idx + 4]
                       if not re.match(r"^\d+\.", l))
        out.append({"num": int(m.group(1)), "text": clean(m.group(2)),
                    "section": section, "answered": answered})
    return out


def run_stat(path, stale_hours, window=None):
    """Last-run time of a scheduled writer, and whether it is overdue."""
    if not path.exists():
        return "--:--", True, None
    ts = path.stat().st_mtime
    late = (time.time() - ts) / 3600 > stale_hours
    if window and not (window[0] <= time.localtime().tm_hour < window[1]):
        late = False
    return time.strftime("%H:%M", time.localtime(ts)), late, ts


RUN_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · ([\w-]+) · (\S[^·]*?)\s*(?:·|$)")


def run_log_last(name):
    """Newest Runs.md line for a run: (epoch, status) or None."""
    path = META / "Runs.md"
    if not name or not path.exists():
        return None
    for line in reversed(path.read_text().splitlines()):
        m = RUN_LINE.match(line)
        if m and m.group(2) == name:
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M"))
            except ValueError:
                return None
            return ts, m.group(3).strip()
    return None


def next_brief():
    now = datetime.datetime.now()
    t = now.replace(hour=BRIEF_AT[0], minute=BRIEF_AT[1], second=0, microsecond=0)
    if t <= now:
        t += datetime.timedelta(days=1)
    return t, t.strftime("%H:%M")


def next_sync():
    now = datetime.datetime.now()
    lo, hi = SYNC_WINDOW
    for h in range(lo, hi + 1, 2):
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t > now:
            return t, t.strftime("%H:%M")
    t = (now + datetime.timedelta(days=1)).replace(
        hour=lo, minute=0, second=0, microsecond=0)
    return t, f"tomorrow {t.strftime('%H:%M')}"


def late_label(ts):
    if ts is None:
        return "no run"
    hours = (time.time() - ts) / 3600
    return f"late {int(hours // 24)}d" if hours >= 48 else f"late {int(hours)}h"


# --------------------------------------------------------------- vault write

def rewrite_task(tid, change):
    """Find the line by id, hand its (mark, body) to change, write the result."""
    lines = TODO.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line[:5] in ("- [ ]", "- [x]", "- [X]") and task_id(line) == tid:
            new = change(line[:5], line_body(line))
            if new is None:
                return None
            lines[i] = new[0] + " " + new[1] + "\n"
            save(TODO, "".join(lines))
            return task_id(lines[i])
    return None


def add_task(section, title, note=""):
    """Append a line to a board section (created before Done when missing). Returns its id."""
    section, title, note = clean(section), clean(title), clean(note)
    if not section or not title or section.startswith("Clocks") or section == "Done":
        return None
    stamp = time.strftime("%m-%d")
    line = f"- [ ] **{title[:60]}**" + (f" - {note}" if note else "") + f" · added {stamp}\n"
    lines = TODO.read_text().splitlines(keepends=True) if TODO.exists() else ["# Todo\n"]
    heads = [i for i, l in enumerate(lines) if l.startswith("## ")]
    at = next((i for i in heads if clean(lines[i][3:]) == section), None)
    if at is None:
        done = next((i for i in heads if clean(lines[i][3:]) == "Done"), len(lines))
        lines[done:done] = [f"## {section}\n", "\n", line, "\n"]
    else:
        end = next((i for i in heads if i > at), len(lines))
        ins = end
        while ins > at + 1 and lines[ins - 1].strip() == "":
            ins -= 1
        lines[ins:ins] = [line]
    save(TODO, "".join(lines))
    return task_id(line)


def edit_task(tid, title, note):
    """Rewrite a line's title and note, keeping its stamps."""
    title, note = clean(title), clean(note)
    if not title:
        return None

    def change(mark, body):
        _, sep, tail = body.partition(" · ")
        head = f"**{title[:60]}**" + (f" - {note}" if note else "")
        return mark, head + (sep + tail if sep else "")
    return rewrite_task(tid, change)


def drop_chunk(body, n):
    chunks = trailing_chunks(body)
    if not 0 <= n < len(chunks):
        return body
    start, end, _ = chunks[n]
    return (body[:start] + body[end:]).rstrip()


def resolve_clock(cid):
    """Stamp a Clocks-section line resolved; the parser then drops it."""
    lines = TODO.read_text().splitlines(keepends=True)
    in_clocks = False
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_clocks = line[3:].strip().startswith("Clocks")
        elif (in_clocks and line.startswith("- ")
              and task_id(line) == cid):
            stamp = time.strftime("%m-%d")
            lines[i] = (line.rstrip("\n")
                        + f" · resolved from dashboard {stamp}\n")
            save(TODO, "".join(lines))
            return True
    return False


def close_task(tid, action, n=0):
    stamp = time.strftime("%m-%d")

    def change(mark, body):
        chunks = trailing_chunks(body)
        last = chunks[-1] if chunks else None
        if action == "done":
            if last and REOPEN_RE.match(last[2]):
                return "- [x]", drop_chunk(body, len(chunks) - 1)
            return "- [x]", f"{body} · closed from dashboard {stamp}"
        if action == "notneeded":
            return "- [x]", f"{body} · closed from dashboard {stamp}: not needed"
        if action == "unsure":
            return mark, f"{body} · flagged unsure via dashboard {stamp}"
        if action == "reframe":
            return mark, f"{body} · reframe asked via dashboard {stamp}"
        if action == "reopen":
            if last and CLOSE_RE.match(last[2]):
                return "- [ ]", drop_chunk(body, len(chunks) - 1)
            return "- [ ]", f"{body} · reopened from dashboard {stamp}"
        if action == "unflag":
            if 0 <= n < len(chunks) and chunks[n][2].lower().startswith("flagged"):
                return mark, drop_chunk(body, n)
            return None
        if action == "unreframe":
            for i, c in enumerate(chunks):
                if REFRAME_RE.match(c[2]):
                    return mark, drop_chunk(body, i)
            return None
        return None
    return rewrite_task(tid, change)


def archive_done():
    """Move every done line (with its continuation lines) to the archive, newest first."""
    if not TODO.exists():
        return 0
    lines = TODO.read_text().splitlines(keepends=True)
    keep, moved, section, i = [], {}, "", 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            section = clean(line[3:])
        if line[:5] in ("- [x]", "- [X]") and section and not section.startswith("Clocks"):
            block, i = [line if line.endswith("\n") else line + "\n"], i + 1
            while i < len(lines) and lines[i].startswith("  "):
                block.append(lines[i])
                i += 1
            moved.setdefault(section, []).append("".join(block))
            continue
        keep.append(line)
        i += 1
    if not moved:
        return 0
    text = ARCHIVE.read_text() if ARCHIVE.exists() else "# Todo archive\n"
    for sec, blocks in moved.items():
        head = f"## {sec}\n"
        chunk = "".join(blocks)
        at = text.find("\n" + head)
        if at == -1:
            text = text.rstrip("\n") + "\n\n" + head + "\n" + chunk
        else:
            pos = at + 1 + len(head)
            text = text[:pos] + "\n" + chunk + text[pos:].lstrip("\n")
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    save(ARCHIVE, text)
    save(TODO, "".join(keep))
    return sum(len(b) for b in moved.values())


def write_intake_answer(path, num, answer):
    lines = path.read_text().splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{num}\.\s", line):
            start = i
            break
    if start is None:
        return False
    end = start + 1
    while end < len(lines) and not re.match(r"^(\d+\.\s|## )", lines[end]):
        end += 1
    stamp = time.strftime("%Y-%m-%d %H:%M")
    lines.insert(end, f"> **A ({stamp}):** {answer.strip()}\n")
    save(path, "".join(lines))
    return True


def write_queue_answer(qid, answer):
    text = QUESTIONS.read_text()
    m = re.search(rf"^## {re.escape(qid)}\b.*$", text, re.M)
    if not m:
        return False
    nxt = text.find("\n## ", m.end())
    insert_at = nxt if nxt != -1 else len(text)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    text = text[:insert_at] + f"\n**A:** {answer.strip()} *(via dashboard, {stamp})*\n" + text[insert_at:]
    save(QUESTIONS, text)
    return True


def append_inbox(line_text):
    text = QUESTIONS.read_text() if QUESTIONS.exists() else ""
    line = f"- {line_text}\n"
    if INBOX_HEADER in text:
        text = text.replace(INBOX_HEADER + "\n", INBOX_HEADER + "\n" + line, 1)
    else:
        text += f"\n{INBOX_HEADER}\n\n*(raw notes dropped from the dashboard; /intake consumes these)*\n\n{line}"
    save(QUESTIONS, text)


def append_note(entry):
    stamp = time.strftime("%Y-%m-%d %H:%M")
    append_inbox(f"{stamp} — {entry.strip()}")


def drop_inbox_line(needle):
    if not QUESTIONS.exists():
        return
    text = QUESTIONS.read_text()
    idx = text.find(INBOX_HEADER)
    if idx == -1:
        return
    head, tail = text[:idx], text[idx:]
    out, dropped = [], False
    for l in tail.splitlines(keepends=True):
        if not dropped and l.startswith("- ") and needle in l:
            dropped = True
            continue
        out.append(l)
    if dropped:
        save(QUESTIONS, head + "".join(out))


# ------------------------------------------------------------- integrations

def attio_get(path, method="GET", body=None):
    token = secret("ATTIO_TOKEN", ATTIO_TOKEN)
    req = urllib.request.Request(
        "https://api.attio.com/v2" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def iso_days(iso):
    """Whole days since an Attio timestamp, or None when it will not parse."""
    try:
        when = datetime.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return max(0, (datetime.datetime.utcnow() - when).days)


def money(cell):
    val = cell.get("currency_value")
    if val in (None, ""):
        return ""
    return f"{int(val):,} {cell.get('currency_code') or ''}".strip()


def attio_company(cid):
    """A company's name by record id, cached for an hour; empty when unknown."""
    key = "co:" + cid
    if key not in _cache:
        try:
            v = attio_get(f"/objects/companies/records/{cid}")["data"]["values"]
            name = (v.get("name") or [{}])[0].get("value") or ""
        except Exception:
            name = ""
        _cache[key] = (time.time(), name)
    return _cache[key][1]


def attio_deals():
    try:
        data = attio_get("/objects/deals/records/query", "POST", {"limit": 100})["data"]
        deals = []
        for rec in data:
            v = rec.get("values", {})
            cell = (v.get("stage") or [{}])[0]
            stage = (cell.get("status") or {}).get("title") or ""
            url = rec.get("web_url") or ""
            cid = (v.get("associated_company") or [{}])[0].get("target_record_id") or ""
            deals.append({
                "id": rec.get("id", {}).get("record_id", ""),
                "company": attio_company(cid) if cid else "",
                "company_id": cid,
                "name": (v.get("name") or [{}])[0].get("value") or "Untitled",
                "stage": re.sub(r"[^\x00-\x7F]", "", stage).strip() or "No stage",
                "value": money((v.get("value") or [{}])[0]),
                "days": iso_days(cell.get("active_from")),
                "url": url if url.startswith(CONFIG["attio_url"] + "/") else "",
            })
        return deals
    except Exception as exc:
        return {"error": str(exc)[:120]}


def attio_next_steps():
    """record_id -> open task text. Absent or failing means no next steps."""
    try:
        steps = {}
        for t in attio_get("/tasks?limit=100").get("data", []):
            if t.get("is_completed"):
                continue
            text = (t.get("content_plaintext") or "").strip()
            if not text:
                continue
            for link in t.get("linked_records") or []:
                rid = link.get("target_record_id")
                if rid and rid not in steps:
                    steps[rid] = text[:120]
        return steps
    except Exception:
        return {}


LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_FIELDS = '{nodes{identifier title url priority sortOrder state{name type} team{name} project{name} assignee{name isMe}}}'


def who_pill(x):
    a = x.get("assignee")
    if not a:
        return '<span class="pill tag warn">unassigned</span>'
    if a.get("isMe"):
        return '<span class="pill tag ok">me</span>'
    return f'<span class="pill tag">{E(a.get("name", ""))}</span>'
LINEAR_MINE = ('{issues(first:30, filter:{state:{type:{nin:["completed","canceled"]}},'
               ' or:[{assignee:{isMe:{eq:true}}},{project:{lead:{isMe:{eq:true}}}}]})'
               + LINEAR_FIELDS + '}')
LINEAR_QUERY = ('{issues(first:30, filter:{state:{type:{nin:["completed","canceled"]}}})'
                + LINEAR_FIELDS + '}')


def linear_ordered(issues):
    """Plan order: Linear priority (1 urgent .. 4 low, 0 unset last), then Linear's own sort."""
    def key(x):
        pr = x.get("priority") or 0
        st = x.get("state") or {}
        rank = 0 if st.get("name") == "In Progress" else (3 if st.get("type") == "started" else 1)
        return (rank, pr if pr else 9, x.get("sortOrder") or 0)
    return sorted((x for x in issues if (x.get("state") or {}).get("type") != "triage"), key=key)


def linear_triage(issues):
    return [x for x in issues if (x.get("state") or {}).get("type") == "triage"]


def linear_count():
    if not on("linear") or not secret("LINEAR_API_KEY", CONFIG["linear_key_path"]):
        return 0
    got = cached("linear", 60, linear_issues)
    return len(got) if isinstance(got, list) else 0


def work_count(c):
    return sum(1 for t in c["items"] if not t["done"]) + linear_count()


def linear_issues():
    """Top active issues. The key never leaves this function."""
    key = secret("LINEAR_API_KEY", CONFIG["linear_key_path"])
    query = LINEAR_MINE if CONFIG["linear_mine"] else LINEAR_QUERY
    req = urllib.request.Request(
        LINEAR_URL, method="POST",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except urllib.error.HTTPError as exc:
        return {"error": f"Linear answered {exc.code}."}
    except Exception:
        return {"error": "Linear did not answer."}
    if data.get("errors"):
        return {"error": "Linear rejected the query."}
    return ((data.get("data") or {}).get("issues") or {}).get("nodes", [])


GH_FIELDS = "repository,title,updatedAt,number,url,state"


def gh_json(args, fields=GH_FIELDS):
    out = subprocess.run(
        ["gh"] + args + ["--json", fields],
        capture_output=True, text=True, timeout=25)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[:120] or "gh call failed")
    return json.loads(out.stdout or "[]")


def gh_build():
    try:
        if subprocess.run(["gh", "auth", "status"], capture_output=True,
                          text=True, timeout=15).returncode:
            return {"error": "GitHub is not connected. Run gh auth login."}
        repo_args = []
        for r in REPOS:
            repo_args += ["--repo", r]
        me = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True,
                            text=True, timeout=15).stdout.strip()
        since = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        review, mine, shipped = [], [], 0
        for repo in REPOS:
            for x in gh_json(["pr", "list", "--repo", repo, "--state", "open", "--limit", "30"],
                             "number,title,url,isDraft,author,reviewRequests,statusCheckRollup,updatedAt"):
                x["repository"] = {"name": repo.split("/")[-1]}
                asked = any((r.get("login") or "") == me for r in x.get("reviewRequests") or [])
                if asked:
                    review.append(x)
                elif (x.get("author") or {}).get("login") == me:
                    mine.append(x)
            shipped += len(gh_json(["pr", "list", "--repo", repo, "--state", "merged", "--limit", "50",
                                    "--search", f"merged:>={since}"], "number"))
        return {"review": [gh_row(x) for x in review], "mine": [gh_row(x) for x in mine],
                "shipped": shipped}
    except Exception as exc:
        return {"error": str(exc)[:120]}


def ci_state(x):
    checks = x.get("statusCheckRollup") or []
    if not checks:
        return ""
    if any((c.get("conclusion") or "").upper() in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for c in checks):
        return "red"
    if any((c.get("status") or c.get("state") or "").upper() not in ("COMPLETED", "SUCCESS") for c in checks):
        return "pending"
    return "green"


def gh_row(x, force=""):
    state = force or ("draft" if x.get("isDraft")
                      else str(x.get("state", "")).lower() or "open")
    return {"repo": x.get("repository", {}).get("name", "?"),
            "title": x.get("title", ""), "num": x.get("number", ""),
            "url": x.get("url", ""), "state": state, "ci": ci_state(x),
            "age": since_label(x.get("updatedAt", ""))}


def since_label(iso):
    try:
        when = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""
    mins = (datetime.datetime.utcnow() - when).total_seconds() / 60
    if mins < 90:
        return "just now"
    if mins < 60 * 36:
        return f"{int(mins // 60)}h"
    return f"{int(mins // 1440)}d"


# ------------------------------------------------------------- handbook read

def hb_roots():
    """Extra handbooks mounted read-only beside the vault: name -> absolute path."""
    out = {}
    for h in CONFIG["handbooks"]:
        name, path = h.get("name", ""), pathlib.Path(os.path.expanduser(h.get("path", "")))
        if name and "/" not in name and path.is_dir():
            out[name] = path.resolve()
    return out


def _walk_md(base, prefix=""):
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in CONFIG["hide_dirs"])
        rel_root = pathlib.Path(root).relative_to(base)
        for f in sorted(files):
            if f.endswith(".md"):
                rel = str(rel_root / f) if str(rel_root) != "." else f
                out.append(prefix + rel)
    return out


def vault_pages():
    """Every .md path in the vault plus the mounted handbooks, as @Name/... paths."""
    out = _walk_md(VAULT)
    for name, path in hb_roots().items():
        out += _walk_md(path, f"@{name}/")
    return out


def page_index():
    """basename (lowercased stem) -> relative path, for wikilink resolution."""
    idx = {}
    for rel in cached("pages", 30, vault_pages):
        idx.setdefault(pathlib.PurePath(rel).stem.lower(), rel)
    return idx


def safe_page(rel):
    """A vault .md path, or None. Refuses anything that escapes the root."""
    if not rel or not rel.endswith(".md"):
        return None
    base = VAULT
    if rel.startswith("@"):
        name, _, rel = rel[1:].partition("/")
        base = hb_roots().get(name)
        if not base or not rel:
            return None
    try:
        p = (base / rel).resolve()
    except OSError:
        return None
    root = str(base.resolve())
    if not (str(p) == root or str(p).startswith(root + os.sep)):
        return None
    return p if p.is_file() else None


def git(repo, args):
    """Read-only git in one repo. Fixed arg list, never a shell."""
    out = subprocess.run(["git", "-C", str(repo)] + args,
                         capture_output=True, text=True, timeout=20)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[:160] or "git call failed")
    return out.stdout


def git_repos():
    """Label -> path, for every configured repo that exists right now."""
    out = {}
    for p in CONFIG["repos_git"]:
        if (p / ".git").exists():
            out[CONFIG["wordmark"] if p == VAULT else p.name] = p
    return out


COMMIT_KINDS = ("daily", "capture", "meta", "dashboard", "monthly", "feat", "fix",
                "chore", "refactor", "docs", "test", "style", "build", "ci", "perf")


def git_log(key, repo, n=LOG_PER_REPO):
    try:
        rows = []
        for line in git(repo, ["log", f"-{int(n)}", "--date=short",
                               "--pretty=format:%h|%ad|%at|%s"]).splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            prefix = re.split(r"[:(]", parts[3], 1)[0].strip().lower()
            rows.append({"hash": parts[0], "date": parts[1],
                         "at": int(parts[2]) if parts[2].isdigit() else 0,
                         "subject": parts[3], "repo": key,
                         "kind": prefix if prefix in COMMIT_KINDS else "other"})
        return rows
    except Exception as exc:
        return {"error": str(exc)[:160]}


def repo_log(key, repo):
    return cached(f"log:{key}", 60, lambda: git_log(key, repo))


def repo_timeline(repos, key):
    """One repo's log, or every repo merged newest first."""
    if key in repos:
        return repo_log(key, repos[key])
    rows = []
    for k, p in repos.items():
        got = repo_log(k, p)
        if isinstance(got, list):
            rows += got
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:LOG_MERGED_MAX]


def git_show(repo, h):
    try:
        return git(repo, ["show", h, "--stat", "--patch"])
    except Exception as exc:
        return {"error": str(exc)[:160]}


LINK_RE = re.compile(r"\[\[([^\]|#]+)")


def vault_graph():
    """Wikilink graph of the vault: nodes are pages, edges are resolved links."""
    pages = _walk_md(VAULT)
    index, nodes, at = {}, [], {}
    for rel in pages:
        stem = pathlib.PurePath(rel).stem
        index.setdefault(stem.lower(), rel)
        at[rel] = len(nodes)
        parts = rel.split(os.sep)
        nodes.append({"id": stem, "path": rel, "links_count": 0,
                      "domain": parts[0] if len(parts) > 1 else "root"})
    seen = set()
    for rel in pages:
        i = at[rel]
        try:
            text = (VAULT / rel).read_text(errors="ignore")
        except OSError:
            continue
        for target in LINK_RE.findall(text):
            dest = index.get(pathlib.PurePath(target.strip()).stem.lower())
            if not dest or dest == rel:
                continue
            j = at[dest]
            pair = (i, j) if i < j else (j, i)
            if pair in seen:
                continue
            seen.add(pair)
            nodes[i]["links_count"] += 1
            nodes[j]["links_count"] += 1
    keep = [i for i, n in enumerate(nodes) if n["links_count"]]
    new = {old: k for k, old in enumerate(keep)}
    nodes = [nodes[i] for i in keep]
    edges = sorted((new[i], new[j]) for i, j in seen)
    key = hashlib.md5(
        (f"v{GRAPH_LAYOUT_VERSION}|" + "\n".join(n["path"] for n in nodes)
         + "|" + ",".join(f"{i}-{j}" for i, j in edges)).encode()).hexdigest()[:16]
    return {"nodes": nodes, "edges": edges, "hash": key}


# ---------------------------------------------------------- minimal markdown

def md_inline(s):
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = E(s.replace("`", ""))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", s)


def md_block(text):
    """Bold, numbered and bulleted lists, wikilinks as plain text. Nothing else."""
    out, buf, mode, start = [], [], None, 1

    def flush():
        nonlocal buf, mode
        if buf:
            tag = mode or "ul"
            attr = f' start="{start}"' if tag == "ol" and start != 1 else ""
            out.append(f"<{tag}{attr}>"
                       + "".join(f"<li>{x}</li>" for x in buf) + f"</{tag}>")
        buf, mode = [], None

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(">"):
            line = re.sub(r"^\[!\w+\]\s*", "", line.lstrip("> ").strip())
        if not line:
            flush()
            continue
        m = re.match(r"^(\d+)[.)]\s+(.*)", line)
        if m:
            if mode != "ol":
                flush()
                mode, start = "ol", int(m.group(1))
            buf.append(md_inline(m.group(2)))
            continue
        if re.match(r"^[-*+]\s+", line):
            if mode != "ul":
                flush()
                mode = "ul"
            buf.append(md_inline(line[2:]))
            continue
        flush()
        out.append(f"<p>{md_inline(line)}</p>")
    flush()
    return "".join(out)


def wikilink(target, label, index):
    stem = target.split("#")[0].strip()
    rel = index.get(pathlib.PurePath(stem).stem.lower())
    if not rel:
        return E(label)
    return f'<a href="/handbook?page={urllib.parse.quote(rel)}">{E(label)}</a>'


def page_inline(s, index):
    codes = []

    def stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"
    s = re.sub(r"`([^`]+)`", stash, s)
    s = E(s)
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]",
               lambda m: wikilink(m.group(1), m.group(2), index), s)
    s = re.sub(r"\[\[([^\]]+)\]\]",
               lambda m: wikilink(m.group(1), m.group(1), index), s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<em>\1</em>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: f"<code>{E(codes[int(m.group(1))])}</code>", s)


FRONT_KEYS = ("canon", "status", "last-reviewed")


def frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        k, _, v = line.partition(":")
        if v.strip():
            meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, text[end + 4:].lstrip("\n")


def md_page(text, index):
    meta, text = frontmatter(text)
    out, lines, i = [], text.splitlines(), 0
    strip = ""
    bits = [f"{k} {meta[k]}" for k in FRONT_KEYS if k in meta]
    if bits:
        strip = f'<div class="meta">{E(" · ".join(bits))}</div>'

    def table(start):
        rows, j = [], start
        while j < len(lines) and lines[j].strip().startswith("|"):
            rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
            j += 1
        if len(rows) < 2 or not set(rows[1][0]) <= set("-: "):
            return None, start
        head = "".join(f"<th>{page_inline(c, index)}</th>" for c in rows[0])
        body = "".join("<tr>" + "".join(f"<td>{page_inline(c, index)}</td>"
                                        for c in r) + "</tr>" for r in rows[2:])
        return (f'<div class="tw"><table><thead><tr>{head}</tr></thead>'
                f"<tbody>{body}</tbody></table></div>"), j

    stack = []

    def close_lists(depth=0):
        while len(stack) > depth:
            out.append(f"</{stack.pop()}>")

    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if line.startswith("```"):
            close_lists()
            j, buf = i + 1, []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            out.append(f"<pre>{E(chr(10).join(buf))}</pre>")
            i = j + 1
            continue
        if line.startswith("|"):
            html_tbl, j = table(i)
            if html_tbl:
                close_lists()
                out.append(html_tbl)
                i = j
                continue
        if line.startswith(">"):
            close_lists()
            buf, kind, j = [], "", i
            while j < len(lines) and lines[j].strip().startswith(">"):
                t = lines[j].strip().lstrip(">").strip()
                k = re.match(r"^\[!(\w+)\]\s*(.*)$", t)
                if k:
                    kind = k.group(1).lower()
                    t = k.group(2)
                if t:
                    buf.append(t)
                j += 1
            label = f'<div class="lab">{E(kind)}</div>' if kind else ""
            body = "".join(f"<p>{page_inline(b, index)}</p>" for b in buf)
            out.append(f'<div class="well {E(kind)}">{label}{body}</div>')
            i = j
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            close_lists()
            n = len(h.group(1))
            out.append(f"<h{n}>{page_inline(h.group(2), index)}</h{n}>")
            i += 1
            continue
        if re.match(r"^(---+|\*\*\*+)$", line):
            close_lists()
            out.append("<hr>")
            i += 1
            continue
        m = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            depth = (len(raw) - len(raw.lstrip())) // 2 + 1
            tag = "ul" if m.group(1) in "-*+" else "ol"
            close_lists(depth)
            while len(stack) < depth:
                out.append(f"<{tag}>")
                stack.append(tag)
            out.append(f"<li>{page_inline(m.group(2), index)}</li>")
            i += 1
            continue
        close_lists()
        if line:
            out.append(f"<p>{page_inline(line, index)}</p>")
        i += 1
    close_lists()
    return strip + "".join(out)


# --------------------------------------------------------------------- shell

def _tile(bg, mark):
    return f'<rect width="16" height="16" rx="4" fill="{bg}"/>{mark}'


ICONS = {
    "granola": _tile("#F5EFE0",
                     '<path d="M3.4 7.4h9.2a4.6 4.6 0 0 1-9.2 0z" fill="#1B1B19"/>'
                     '<path d="M5.6 5.6a2.4 2.4 0 0 1 4.8 0" fill="none" '
                     'stroke="#1B1B19" stroke-width="1.1"/>'),
    "claude": _tile("#D97757",
                    '<path d="M8 2.9c.56 2.96 1.95 4.35 4.9 4.9-2.95.56-4.34 1.95-4.9 4.9'
                    '-.56-2.95-1.95-4.34-4.9-4.9 2.95-.55 4.34-1.94 4.9-4.9z" fill="#fff"/>'),
    "dashboard": _tile("#1B1B19",
                       '<rect x="4.6" y="4.6" width="6.8" height="6.8" fill="none" '
                       'stroke="#E8E7E3" stroke-width="1.3"/>'),
    "mail": _tile("#6E6D68",
                  '<rect x="3.3" y="5.1" width="9.4" height="5.8" fill="none" '
                  'stroke="#fff" stroke-width="1.2"/>'
                  '<path d="M3.7 5.5 8 8.8l4.3-3.3" fill="none" stroke="#fff" '
                  'stroke-width="1.2" stroke-linejoin="round"/>'),
}

ASSET_DIRS = (_VAULT / ".dashboard" / "assets", ASSET_DIR)
ASSET_ICONS = {p.stem: p.name for d in reversed(ASSET_DIRS) if d.is_dir()
               for p in d.glob("*.png")}


def asset_path(name):
    for d in ASSET_DIRS:
        try:
            p = (d / name).resolve()
        except (OSError, ValueError):
            continue
        if str(p).startswith(str(d.resolve()) + os.sep) and p.is_file():
            return p
    return None

ICON_DEFS = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
             + "".join(f'<symbol id="i-{k}" viewBox="0 0 16 16">{v}</symbol>'
                       for k, v in ICONS.items())
             + "</svg>")

GIT_MARKS = {
    "open": '<circle cx="8" cy="8" r="5" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    "draft": ('<circle cx="8" cy="8" r="5" fill="none" stroke="currentColor" '
              'stroke-width="1.5" stroke-dasharray="2.4 2"/>'),
    "merged": ('<circle cx="8" cy="8" r="5.6" fill="currentColor"/>'
               '<path d="m5.6 8.1 1.7 1.8 3.1-3.6" fill="none" stroke="var(--panel)" '
               'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'),
}

GIT_MARK_DEFS = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
                 + "".join(f'<symbol id="g-{k}" viewBox="0 0 16 16">{v}</symbol>'
                           for k, v in GIT_MARKS.items())
                 + "</svg>")

DOMAIN_TONES = CONFIG.get("domain_tones", {})

CSS = """
:root{
  --chassis:#E8E7E3; --panel:#F4F3F0; --panel-2:#ECEBE7;
  --ink:#1B1B19; --ink-2:#6E6D68; --line:#C9C8C2; --line-soft:#DBDAD5;
  --accent:__ACCENT__; --ok:#3D7A4A; --warn:#B0790A; --diff-del:#A8392B;
  --add-bg:rgba(61,122,74,.12); --del-bg:rgba(168,57,43,.12);
  --on-accent:#1B1B19; --dot:rgba(27,27,25,.055);
  --sans:'Space Grotesk',system-ui,-apple-system,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark){:root{
  --chassis:#161615; --panel:#1E1E1C; --panel-2:#191918;
  --ink:#E9E8E4; --ink-2:#8B8A85; --line:#33332F; --line-soft:#282825;
  --accent:__ACCENT_DARK__; --ok:#5FA36E; --warn:#D0972B; --diff-del:#D0705E;
  --add-bg:rgba(95,163,110,.14); --del-bg:rgba(208,112,94,.14);
  --on-accent:#161615; --dot:rgba(233,232,228,.05);
}}
*{box-sizing:border-box;margin:0;}
body{background:var(--chassis);color:var(--ink);
  background-image:radial-gradient(var(--dot) 1px,transparent 1px);
  background-size:24px 24px;
  font:400 14.5px/1.55 var(--sans);-webkit-font-smoothing:antialiased;}
[hidden]{display:none!important;}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
a{color:inherit;}
.mono{font-family:var(--mono);}
.lab{font:500 11px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2);}
.ic{width:16px;height:16px;flex:none;vertical-align:-3px;display:block;}
.ic.tile{border-radius:4px;border:1px solid var(--line);object-fit:cover;}
.gm{width:14px;height:14px;flex:none;color:var(--ink-2);align-self:center;}
.gm.merged{color:var(--ok);}
.live{font:500 10px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  color:var(--ok);border:1px solid var(--ok);border-radius:4px;padding:3px 5px;flex:none;}

.shell{display:grid;grid-template-columns:212px minmax(0,1fr);
  max-width:1240px;margin:0 auto;gap:0;}
.rail{border-right:1px solid var(--line);padding:26px 16px 40px 20px;
  position:sticky;top:0;align-self:start;max-height:100vh;overflow:auto;}
.main{padding:26px 20px 90px;min-width:0;}
.wordmark{font:700 19px/1 var(--sans);letter-spacing:-.01em;display:flex;flex-direction:column;align-items:flex-start;gap:9px;}
.logo{height:18px;width:auto;display:block;}
.stamp{font:400 11px/1 var(--mono);color:var(--ink-2);display:block;margin-top:6px;}
.runs{display:flex;flex-direction:column;margin:16px 0 0;}
.rail>.keys{margin-top:22px;}
.run{border:1px solid var(--line);padding:8px 11px 9px;margin-top:-1px;
  background:var(--panel);}
.run .v{font:500 17px/1.2 var(--mono);margin-top:5px;}
.run .c{font:400 10.5px/1.3 var(--mono);color:var(--ink-2);margin-top:4px;}
.run.warn .v{color:var(--warn);}
.dot{width:6px;height:6px;border-radius:50%;background:var(--warn);flex:none;
  display:inline-block;}

.keys{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:6px;
  overflow:hidden;}
.key{border:0;border-top:1px solid var(--line);background:var(--panel);color:var(--ink-2);
  font:500 13px/1.3 var(--sans);padding:10px 12px;cursor:pointer;display:flex;gap:8px;
  align-items:center;width:100%;text-align:left;
  transition:background .12s ease-out,color .12s ease-out;}
.key:first-child{border-top:0;}
.key:hover{color:var(--ink);}
.key.on{background:var(--accent);color:var(--on-accent);}
.key:active{transform:translateY(1px);}
.key .n{margin-left:auto;font:400 11px/1 var(--mono);opacity:.8;}
.key .d{font:400 10px/1 var(--mono);opacity:.55;width:8px;}
.keys.row{flex-direction:row;width:fit-content;}
.keys.row .key{border-top:0;border-left:1px solid var(--line);width:auto;
  justify-content:center;text-decoration:none;}
.keys.row .key:first-child{border-left:0;}
.keys.sm .key{padding:7px 10px;font-size:12px;white-space:nowrap;min-width:76px;}
.keys.sm .key .n{margin-left:6px;}
@keyframes keypulse{
  0%{box-shadow:inset 0 0 0 1px var(--accent);}
  100%{box-shadow:inset 0 0 0 1px transparent;}}
.key.pulse{animation:keypulse .42s ease-out;}
.wordmark{cursor:pointer;}

.tab{display:none;transition:opacity .15s ease-out;}
.tab.on{display:block;animation:tabin .15s ease-out;}
.fade{opacity:0;}
.oops{font:400 11.5px/1.5 var(--mono);color:var(--diff-del);margin-top:4px;}
.pend{opacity:.5;}
@keyframes tabin{from{opacity:0;transform:translateY(2px)}to{opacity:1;transform:none}}
.panel{background:var(--panel);border:1px solid var(--line);padding:16px;}
.panel+.panel{margin-top:-1px;}
.panel.flush{padding:0;}
.phead{display:flex;align-items:center;gap:12px;padding-bottom:12px;flex-wrap:wrap;}
.phead h2{font:500 11px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-2);}
.hctl{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-left:auto;
  justify-content:flex-end;}
.phead .count{font:400 11px/1 var(--mono);color:var(--ink-2);text-align:right;
  min-width:58px;flex:none;}
.pcap{color:var(--ink-2);font-size:12.5px;margin:-6px 0 12px;}
.addwrap{padding:0 0 10px;}
.addwrap .reframe form{display:flex;gap:8px;flex-wrap:wrap;}
.addwrap .reframe select{max-width:180px;}
.bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:2px 0 12px;
  border-bottom:1px solid var(--line-soft);margin-bottom:8px;}
.bar .pills{margin-left:auto;}
.pills{display:flex;gap:6px;flex-wrap:wrap;}
.pill{border:1px solid var(--line);border-radius:999px;background:var(--panel);
  color:var(--ink-2);font:500 11.5px/1 var(--sans);padding:6px 10px;cursor:pointer;
  transition:color .12s ease-out,border-color .12s ease-out;}
.pill:hover{color:var(--ink);border-color:var(--ink-2);}
.pill.on{color:var(--on-accent);background:var(--accent);border-color:var(--accent);}
.pill.tag{cursor:default;padding:4px 8px;font:400 10.5px/1 var(--mono);letter-spacing:.04em;}
.pill.tag.warn{color:var(--warn);border-color:var(--warn);}
.pill.tag.ok{color:var(--ok);border-color:var(--ok);}
.gsort{margin-left:auto;font:400 11px/1 var(--mono);color:var(--ink-2);background:transparent;
  border:0;cursor:pointer;padding:0 22px 0 0;}
.pblock.done .rows{opacity:.7;}
.pblock.done .gtog .btn{margin-left:10px;font-size:11px;padding:3px 8px;}
@media (hover:hover){.row .mk,.row .cmt{opacity:0;transition:opacity .12s ease-out;}
  .row:hover .mk,.row:hover .cmt,.row:focus-within .mk,.row:focus-within .cmt,
  .row .mk[aria-expanded="true"]{opacity:1;}}
.cmt{border:0;background:transparent;color:var(--ink-2);cursor:pointer;
  font:500 11px/1.3 var(--mono);padding:4px 6px;flex:none;align-self:center;}
.cmt:hover{color:var(--accent);}
.ic.tile{filter:saturate(.3);opacity:.8;}
.row:hover .ic.tile{filter:none;opacity:1;}
.out{font:400 11px/1 var(--mono);color:var(--ink-2);text-decoration:none;
  border-bottom:1px solid var(--line);padding-bottom:2px;}
.out:hover{color:var(--accent);border-color:var(--accent);}

.panel.anchor{padding:26px 24px 22px;}
.anchor{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;
  flex-wrap:wrap;}
.anchor h1{font:700 36px/1.08 var(--sans);letter-spacing:-.025em;}
.anchor p{color:var(--ink-2);font-size:14.5px;margin-top:8px;}
.anchor .day{min-width:260px;}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));}
.stat{padding:2px 16px;border-left:1px solid var(--line-soft);
  display:grid;grid-template-rows:14px auto auto;align-content:start;}
.stats{align-items:start;}
.stat:first-child{border-left:0;padding-left:0;}
.stat .v{font:500 28px/1.15 var(--mono);margin-top:9px;letter-spacing:-.02em;}
.stat.warn .v{color:var(--warn);}
.stat .c{font-size:12.5px;color:var(--ink-2);margin-top:6px;}

.rows>*{border-top:1px solid var(--line-soft);}
.rows>*:first-child{border-top:0;}
.row{display:flex;gap:12px;align-items:flex-start;padding:10px 0;min-height:40px;}
.row .body{flex:1;min-width:0;}
.line{display:flex;gap:14px;align-items:baseline;}
.line .t{flex:1;font-size:14.5px;line-height:1.45;}
.meta-r{display:grid;grid-template-columns:16px 40px;gap:6px;align-items:center;
  font:400 11px/1.5 var(--mono);color:var(--ink-2);white-space:nowrap;flex:none;}
.meta-r>*:only-child{grid-column:2;}
.meta-r .ic{grid-column:1;}
.meta-r span{grid-column:2;text-align:right;overflow:hidden;text-overflow:ellipsis;}
.prov{font:400 11px/1.5 var(--mono);color:var(--ink-2);white-space:nowrap;
  max-width:230px;overflow:hidden;text-overflow:ellipsis;}
.sub{color:var(--ink-2);font-size:12.5px;margin-top:4px;}
.group{font:700 20px/1.25 var(--sans);padding:26px 0 10px;display:flex;gap:10px;
  align-items:baseline;border-top:1px solid var(--line);margin-top:16px;
  letter-spacing:-.015em;}
.group:first-child{border-top:0;margin-top:0;padding-top:2px;}
.group .n{font:400 12px/1 var(--mono);color:var(--ink-2);}
button.group{width:100%;background:none;color:inherit;border:0;text-align:left;
  cursor:pointer;}
.chev{margin-left:auto;font:400 13px/1 var(--mono);color:var(--ink-2);}
.gtog[aria-expanded=true] .chev::before{content:"-";}
.gtog[aria-expanded=false] .chev::before{content:"+";}
.gtog:hover .chev{color:var(--ink);}
.pblock .rows{overflow:hidden;transition:max-height .15s ease-out;}
.pblock.shut .rows{max-height:0;}
.empty{color:var(--ink-2);font-size:13.5px;padding:2px 0;}
.saved{font:500 12px/1 var(--mono);color:var(--ok);padding:6px 0;}

.state{display:flex;gap:10px;align-items:baseline;margin-top:5px;
  font:400 12px/1.5 var(--mono);color:var(--ink-2);flex-wrap:wrap;}
.state .w{max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.state form{display:inline;}
.undo{border:0;background:none;padding:0;cursor:pointer;color:var(--accent);
  font:400 12px/1.5 var(--mono);border-bottom:1px solid var(--accent);}

.cb{display:block;}
.gap{width:18px;flex:none;}
.rows.next .gap{width:15px;}
.box{width:18px;height:18px;padding:0;flex:none;margin-top:1px;border-radius:6px;
  border:1px solid var(--ink-2);background:var(--panel-2);cursor:pointer;
  transition:border-color .12s ease-out;display:block;}
.tick{width:100%;height:100%;display:block;}
.tick path{fill:none;stroke:transparent;stroke-width:2.1;stroke-linecap:round;
  stroke-linejoin:round;stroke-dasharray:14;stroke-dashoffset:14;}
.box:hover{border-color:var(--accent);}
.box:hover .tick path{stroke:var(--accent);stroke-dashoffset:0;opacity:.5;}
.row.done .t{color:var(--ink-2);}
.row.done .box{background:var(--accent);border-color:var(--accent);}
.row.done .box .tick path{stroke:var(--on-accent);stroke-dashoffset:0;opacity:1;}
@keyframes tickdraw{from{stroke-dashoffset:14;}to{stroke-dashoffset:0;}}
@keyframes tonefade{from{background:color-mix(in oklab,var(--accent) 13%,transparent);}
  to{background:transparent;}}
.row.done.just .box .tick path{animation:tickdraw .14s ease-out;}
.row.done.just{animation:tonefade .15s ease-out;}

details.more{margin-top:5px;}
details.more summary{list-style:none;cursor:pointer;
  font:400 11px/1.6 var(--mono);color:var(--ink-2);}
details summary::-webkit-details-marker{display:none;}
details.more summary::before{content:"+" attr(data-n) " lines";}
details.more[open] summary::before{content:"less";}
details.more ul.mlines{margin:6px 0 2px;padding-left:16px;list-style:disc;}
details.more ul.mlines li{color:var(--ink-2);font-size:13px;line-height:1.55;
  margin:3px 0;}

.mk,.mkgap{width:52px;flex:none;align-self:center;}
.mk{border:1px solid var(--line);background:var(--panel);color:var(--ink-2);
  border-radius:6px;padding:4px 0;cursor:pointer;text-align:center;
  font:500 11px/1.3 var(--mono);
  transition:color .12s ease-out,border-color .12s ease-out;}
.mk:hover{color:var(--ink);border-color:var(--ink-2);}
.mk:active{transform:translateY(1px);}
.mk[aria-expanded=true]{color:var(--ink);border-color:var(--ink-2);
  background:var(--panel-2);}
.acts{display:none;}
.acts.open{display:block;width:100%;}
.acts-in{display:flex;gap:6px;margin-top:9px;flex-wrap:wrap;align-items:center;}
.acts-in form{display:inline;}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink-2);
  border-radius:6px;font:500 12px/1 var(--mono);padding:6px 11px;cursor:pointer;}
.btn:hover{color:var(--ink);border-color:var(--ink-2);}
.btn:active{transform:translateY(1px);}
.btn.pri{background:var(--accent);border-color:var(--accent);color:var(--on-accent);}
.btn.pri:hover{color:var(--on-accent);border-color:var(--accent);}
select{background:var(--panel-2);border:1px solid var(--line);border-radius:6px;
  color:var(--ink);font:400 12px/1 var(--mono);padding:7px 9px;}
textarea,input[type=text]{width:100%;background:var(--panel-2);color:var(--ink);
  border:1px solid var(--line);border-radius:6px;padding:10px 12px;
  font:400 13px/1.55 var(--mono);resize:vertical;}
textarea:focus,input[type=text]:focus{outline:none;border-color:var(--accent);}
::placeholder{color:var(--ink-2);opacity:1;}
.reframe{display:none;margin-top:8px;max-width:520px;}
.reframe.open{display:block;}
.reframe input[type=text]{font-size:12.5px;padding:9px 12px;}
.reframe .btn{margin-top:8px;}

.two{display:grid;grid-template-columns:minmax(0,1fr);gap:0;}
@media (min-width:1280px){.two{grid-template-columns:minmax(0,1fr) 330px;gap:20px;}}
.side .group{font:500 13px/1.3 var(--sans);padding:16px 0 6px;margin-top:8px;
  letter-spacing:0;}
.side .group:first-child{padding-top:0;margin-top:0;}
.side .line{gap:10px;}
.side .line .t{font-size:13px;line-height:1.4;}

.q{border-top:1px solid var(--line-soft);}
.q:first-child{border-top:0;}
.q>summary{list-style:none;cursor:pointer;padding:14px 0;display:block;}
.q>summary::-webkit-details-marker{display:none;}
.qhead{display:flex;gap:12px;align-items:baseline;}
.qhead h3{font:500 16px/1.35 var(--sans);flex:1;}
.qfirst{color:var(--ink-2);font-size:12.5px;margin-top:5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.qbody{margin:2px 0 12px;font-size:14.5px;line-height:1.6;}
.qbody p{margin:9px 0;}
.qbody ol,.qbody ul{margin:9px 0 9px 22px;}
.qbody li{margin:4px 0;padding-left:2px;}
.qbody strong{font-weight:700;}
.qsec{font:700 17px/1.3 var(--sans);padding:16px 0 4px;}
.form-act{margin-top:8px;padding-bottom:14px;}

.inl{display:inline-block;margin-left:8px;}
.setup-link{display:block;margin-top:14px;text-decoration:none;color:var(--ink-2);}
.setup-link:hover{color:var(--accent);}
@media (max-width:720px){
  .anchor{flex-direction:column;align-items:flex-start;}
}

.totals{display:flex;gap:22px;flex-wrap:wrap;padding:0 0 12px;}
.totals .tot .lab{font:500 11px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2);}
.totals .tot .v{font:500 20px/1.2 var(--mono);margin-top:6px;letter-spacing:-.02em;}
.deals .card{display:flex;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--line-soft);
  text-decoration:none;color:inherit;flex-wrap:wrap;}
.deals .card:first-child{border-top:0;}
.deals .card:hover{background:transparent;}
.deals .card:hover .nm{color:var(--accent);}
.deals .card .nm{font:400 13.5px/1.4 var(--sans);flex:1 1 200px;min-width:0;color:inherit;text-decoration:none;}
.deals .card .nm b{font-weight:600;}
.deals .card .nm:hover{color:var(--accent);}
.deals .card .cm{display:flex;gap:12px;align-items:baseline;font:400 11.5px/1.4 var(--mono);color:var(--ink-2);margin:0;}
.deals .card .cm .mono{color:var(--ink);}
.deals .card .nx{flex-basis:100%;font-size:12.5px;color:var(--ink-2);padding-left:0;}
.deals .card.lost{display:none;}
.deals.showlost .card.lost{display:flex;opacity:.6;}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  border-top:1px solid var(--line-soft);margin:0 -16px -16px;}
.col{border-left:1px solid var(--line-soft);padding:0 0 6px;min-width:0;}
.col:first-child{border-left:0;}
.colhead{display:flex;gap:8px;align-items:baseline;padding:12px 14px 8px;}
.colhead .n{font:400 11px/1 var(--mono);color:var(--ink-2);margin-left:auto;}
.card{display:block;text-decoration:none;color:inherit;padding:9px 14px;
  border-top:1px solid var(--line-soft);}
.card:hover{background:var(--panel-2);}
.card .nm{font:500 13.5px/1.4 var(--sans);display:block;}
.card .cm{display:flex;gap:10px;align-items:baseline;margin-top:4px;
  font:400 11px/1.4 var(--mono);}
.card .cm .age{color:var(--ink-2);margin-left:auto;}
@media (max-width:720px){
  .col{border-left:0;border-top:1px solid var(--line);}
  .col:first-child{border-top:0;}
}

.rows.next .row{padding:6px 0;min-height:0;gap:10px;}
.rows.next .line .t{font-size:13px;line-height:1.4;}
.rows.next .box{width:15px;height:15px;border-radius:5px;}
.rows.next .mk,.rows.next .mkgap{display:none;}
.rows.next .sub{font-size:12px;}
.sub.dk{font:400 11px/1.5 var(--mono);letter-spacing:.02em;}
.pfx{font:400 11.5px/1.4 var(--mono);color:var(--ink-2);}
.ro{color:var(--ink-2);}

.hb{display:grid;grid-template-columns:minmax(0,1fr);}
@media (min-width:960px){.hb{grid-template-columns:246px minmax(0,1fr);}}
.tree{border-right:1px solid var(--line);padding:14px 14px 30px;overflow:auto;
  max-height:82vh;}
.tree details{margin:0;}
.tree summary{list-style:none;cursor:pointer;font:500 12px/1.9 var(--mono);
  color:var(--ink);padding:1px 0;}
.tree summary::-webkit-details-marker{display:none;}
.tree summary::before{content:"+ ";color:var(--ink-2);}
.tree details[open]>summary::before{content:"- ";}
.tree>details>div,.tree details details{padding-left:12px;}
.tree a{display:block;font:400 12px/1.85 var(--mono);color:var(--ink-2);
  text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.tree a:hover{color:var(--ink);}
.tree a.on{color:var(--accent);}
.doc{padding:18px 22px 60px;min-width:0;max-width:760px;}
.doc .meta{font:400 11px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;
  color:var(--ink-2);padding-bottom:16px;border-bottom:1px solid var(--line-soft);
  margin-bottom:20px;}
.doc h1{font:700 30px/1.15 var(--sans);letter-spacing:-.02em;margin:26px 0 12px;}
.doc h2{font:700 21px/1.25 var(--sans);letter-spacing:-.015em;margin:28px 0 10px;}
.doc h3{font:500 17px/1.3 var(--sans);margin:22px 0 8px;}
.doc h4,.doc h5,.doc h6{font:500 14.5px/1.4 var(--sans);margin:18px 0 6px;}
.doc h1:first-child,.doc h2:first-child{margin-top:0;}
.doc p{margin:11px 0;}
.doc ul,.doc ol{margin:11px 0 11px 22px;}
.doc li{margin:5px 0;}
.doc li>ul,.doc li>ol{margin:4px 0 4px 18px;}
.doc a{color:var(--accent);text-decoration:none;border-bottom:1px solid var(--line);}
.doc a:hover{border-color:var(--accent);}
.doc code{font:400 12.5px/1.4 var(--mono);background:var(--panel-2);
  border:1px solid var(--line-soft);border-radius:4px;padding:1px 4px;}
.doc pre{font:400 12px/1.6 var(--mono);background:var(--panel-2);
  border:1px solid var(--line-soft);padding:12px 14px;overflow-x:auto;margin:14px 0;}
.doc hr{border:0;border-top:1px solid var(--line);margin:24px 0;}
.doc .well{background:var(--panel-2);border-left:2px solid var(--line);
  padding:12px 14px;margin:14px 0;}
.doc .well.todo{border-left-color:var(--warn);}
.doc .well.stale{border-left-color:var(--diff-del);}
.doc .well .lab{margin-bottom:6px;}
.doc .well p{margin:5px 0;font-size:13.5px;color:var(--ink-2);}
.tw{overflow-x:auto;margin:16px 0;}
.doc table{border-collapse:collapse;font:400 12.5px/1.5 var(--mono);width:100%;}
.doc th,.doc td{border:1px solid var(--line-soft);padding:7px 10px;text-align:left;
  vertical-align:top;}
.doc th{font-weight:500;color:var(--ink-2);}
.cwell{padding:18px 0 0;border-top:1px solid var(--line);margin-top:30px;}

.hb{position:relative;}
.hb.wide{grid-template-columns:minmax(0,1fr);}
.hb.wide .tree{display:none;}
.hb.wide .doc{max-width:880px;margin:0 auto;}
.hbtog{position:absolute;top:10px;left:246px;transform:translateX(-50%);z-index:2;
  width:22px;height:26px;padding:0;cursor:pointer;border-radius:6px;
  border:1px solid var(--line);background:var(--panel);color:var(--ink-2);
  display:flex;align-items:center;justify-content:center;}
.hbtog:hover{color:var(--ink);border-color:var(--ink-2);}
.hbtog:active{transform:translateX(-50%) translateY(1px);}
.hbtog svg{width:12px;height:12px;display:block;
  transition:transform .12s ease-out;}
.hb.wide .hbtog{left:10px;transform:none;}
.hb.wide .hbtog:active{transform:translateY(1px);}
.hb.wide .hbtog svg{transform:rotate(180deg);}
@media (max-width:959px){.hbtog{display:none;}}
.treetog{display:none;}

.cday{font:500 11px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-2);padding:14px 16px 8px;border-top:1px solid var(--line);
  background:var(--panel-2);}
.cday:first-child{border-top:0;}
.chip.repo{color:var(--ink);border-color:var(--ink-2);min-width:78px;}
.chip{font:400 10.5px/1 var(--mono);letter-spacing:.06em;color:var(--ink-2);
  border:1px solid var(--line);border-radius:4px;padding:3px 5px;flex:none;
  min-width:62px;text-align:center;}
.cres{margin-top:6px;}
.cres .undo{font:400 11px/1 var(--mono);}
.graph{position:relative;border-top:1px solid var(--line);}
.graph canvas{display:block;width:100%;height:600px;cursor:grab;touch-action:none;}
.graph canvas:active{cursor:grabbing;}
.gtip{position:absolute;pointer-events:none;background:var(--panel-2);
  border:1px solid var(--line);padding:5px 8px;font:400 11.5px/1.3 var(--mono);
  color:var(--ink);white-space:nowrap;display:none;}
.glegend{display:flex;gap:14px;flex-wrap:wrap;padding:12px 16px;
  border-top:1px solid var(--line-soft);font:400 11px/1 var(--mono);color:var(--ink-2);}
.glegend span{display:flex;gap:6px;align-items:center;}
.glegend i{width:8px;height:8px;border-radius:50%;display:block;}

.panel.repos{padding:10px 16px;}
.panel.repos .keys{max-width:100%;overflow-x:auto;}
.commits{padding:0;}
.commit{display:flex;gap:14px;align-items:center;padding:9px 16px;
  border-top:1px solid var(--line-soft);font:400 12.5px/1.5 var(--mono);
  color:var(--ink);text-decoration:none;}
.commit:first-child{border-top:0;}
.cday+.commit{border-top:1px solid var(--line-soft);}
.commit:hover{background:var(--panel-2);}
.commit .h,.commit .d{color:var(--ink-2);}
.commit.on .h{color:var(--accent);}
.commit .s{flex:1;font-family:var(--sans);font-size:13.5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.commit.on{background:var(--panel-2);}
.diff{font:400 12px/1.65 var(--mono);overflow-x:auto;}
.diff .l{white-space:pre-wrap;word-break:break-word;padding:0 16px;}
.diff .a{background:var(--add-bg);}
.diff .d{background:var(--del-bg);color:var(--diff-del);}
.diff .hunk{color:var(--ink-2);padding:8px 16px 2px;white-space:pre-wrap;}
.diff .fh{font:500 12.5px/1 var(--mono);padding:14px 16px 12px;
  border-top:1px solid var(--line);border-bottom:1px solid var(--line-soft);
  margin-top:10px;background:var(--panel-2);word-break:break-all;}
.diff .fh:first-child{margin-top:0;border-top:0;}
.stat-lines{font:400 12px/1.7 var(--mono);color:var(--ink-2);padding:14px 16px;
  border-bottom:1px solid var(--line);white-space:pre-wrap;}

@media (max-width:900px){
  .shell{grid-template-columns:minmax(0,1fr);}
  .rail{border-right:0;border-bottom:1px solid var(--line);position:static;
    max-height:none;padding:20px 20px 16px;}
  .runs{flex-direction:row;margin:14px 0 16px;}
  .run{margin:0 0 0 -1px;flex:1;}
  .keys{flex-direction:row;width:100%;overflow-x:auto;}
  .keys .key{border-top:0;border-left:1px solid var(--line);width:auto;flex:1;}
  .keys .key:first-child{border-left:0;}
  .key .n,.key .d{margin-left:6px;}
  .main{padding:18px 20px 80px;}
  .tree{border-right:0;border-bottom:1px solid var(--line);max-height:280px;}
  .doc{padding:18px 16px 40px;}
}
@media (max-width:720px){
  body{font-size:16px;}
  input,textarea,select{font-size:16px;}
  .stats{grid-template-columns:1fr 1fr;column-gap:16px;}
  .stat{padding:8px 0;border-left:0;}
  .prov{max-width:120px;}
  .anchor h1{font-size:28px;}
  .graph,.glegend{display:none;}
  .rail{padding:14px 16px 10px;}
  .rail .keys:not(.row){position:fixed;left:0;right:0;bottom:0;z-index:60;
    flex-direction:row;border:0;border-top:1px solid var(--line);border-radius:0;
    background:var(--panel);margin:0;overflow-x:auto;
    padding-bottom:env(safe-area-inset-bottom);}
  .rail .keys:not(.row) .key{flex:1;border-left:1px solid var(--line-soft);
    border-top:0;padding:13px 4px;font-size:10.5px;text-align:center;min-width:0;
    justify-content:center;white-space:nowrap;}
  .rail .keys:not(.row) .key .n{display:none;}
  .panel.anchor{padding:20px 16px 16px;}
  .bar .pills{margin-left:0;}
  .rail .keys:not(.row) .key:first-child{border-left:0;}
  .rail .keys:not(.row) .d{display:none;}
  .main{padding:16px 16px 130px;}
  .row{padding:12px 0;gap:12px;}
  .line{flex-wrap:wrap;}
  .line .t{flex:1 1 100%;}
  .cmt{display:none;}
  .mk,.mkgap{width:44px;}
  .box{width:22px;height:22px;border-radius:7px;}
  .treetog{display:block;width:100%;text-align:left;background:var(--panel-2);
    border:0;border-bottom:1px solid var(--line);color:var(--ink-2);cursor:pointer;
    font:500 12px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
    padding:14px 16px;}
  .hb .tree{display:none;}
  .hb.open .tree{display:block;max-height:55vh;}
  .doc{padding:20px 16px 60px;font-size:16px;line-height:1.7;}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;}}
"""

JS = """
function tab(name,focus){
  document.querySelectorAll('.key[data-t]').forEach(k=>{const on=k.dataset.t===name;
    k.classList.toggle('on',on);k.setAttribute('aria-selected',on?'true':'false');});
  document.querySelectorAll('.tab').forEach(s=>s.classList.toggle('on',s.id==='t-'+name));
  history.replaceState(null,'','#'+name);
  const c=document.getElementById('capture');
  if(focus&&name==='inbox'&&c)c.focus();
}
function sortRows(block,v){
  const r=block.querySelector('.rows');if(!r)return;
  const rows=[...r.children];
  if(!r.dataset.init){rows.forEach((x,i)=>x.dataset.i=i);r.dataset.init=1;}
  rows.sort((a,b)=>v==='board'?a.dataset.i-b.dataset.i:
    (v==='new'?1:-1)*((+a.dataset.age||9999)-(+b.dataset.age||9999)));
  rows.forEach(x=>r.appendChild(x));
}
function setSort(sel){
  const b=sel.closest('.pblock');sortRows(b,sel.value);
  try{const m=JSON.parse(localStorage.getItem('hbsort')||'{}');m[b.dataset.p]=sel.value;
    localStorage.setItem('hbsort',JSON.stringify(m));}catch(e){}
}
function restoreSort(){
  let m={};try{m=JSON.parse(localStorage.getItem('hbsort')||'{}');}catch(e){}
  document.querySelectorAll('#t-tasks .pblock').forEach(b=>{
    const v=m[b.dataset.p];const sel=b.querySelector('.gsort');
    if(v&&sel){sel.value=v;sortRows(b,v);}
  });
}
function setPill(btn){
  const c=btn.closest('.panel');c.dataset.p=btn.dataset.p||'';
  c.querySelectorAll('.pills .pill').forEach(b=>b.classList.toggle('on',b===btn));
  refilter(c);
}
function togLost(btn){const d=btn.closest('.panel').querySelector('.deals');
  const on=d.classList.toggle('showlost');btn.classList.toggle('on',on);}
function toggleReframe(id){document.getElementById('rf-'+id).classList.toggle('open');}
function toggleEdit(id){document.getElementById('ed-'+id).classList.toggle('open');}
function toggleAdd(){document.getElementById('addtask').classList.toggle('open');}
function toggleActs(btn){
  const a=btn.closest('.body').querySelector('.acts');
  const on=a.classList.toggle('open');
  btn.setAttribute('aria-expanded',on?'true':'false');
}
function hbpane(){return document.getElementById('t-handbook');}
function hbkey(){
  const cur=document.querySelector('.key.on[data-t]')?.dataset.t;
  const doc=hbpane()?.querySelector('.doc');
  if(cur==='handbook'&&doc&&!doc.querySelector('.empty'))nav('/handbook?tree=1');
  else tab('handbook');
}
function swapIn(el,html){
  el.classList.add('fade');
  el.innerHTML=html;
  requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.remove('fade')));
}
function nav(href,push){
  const pane=hbpane(); if(!pane)return;
  fetch(href+(href.includes('?')?'&':'?')+'partial=1')
    .then(r=>r.ok?r.text():Promise.reject(0))
    .then(h=>{
      swapIn(pane,h);
      if(push!==false)history.pushState({hb:href},'',href);
      tab('handbook'); restoreWide(); graph();
    })
    .catch(()=>{location.href=href;});
}
function restoreWide(){
  const w=document.getElementById('hbtog');
  try{if(w&&localStorage.getItem('hbwide'))hbtog(w,false);}catch(e){}
}
document.addEventListener('click',e=>{
  const a=e.target.closest&&e.target.closest('a[href^="/handbook"]');
  if(!a||e.metaKey||e.ctrlKey||e.shiftKey||e.button)return;
  if(!hbpane())return;
  e.preventDefault();
  nav(a.getAttribute('href'));
});
addEventListener('popstate',()=>{
  if(location.pathname==='/handbook')nav(location.pathname+location.search,false);
});
function hbtog(btn,save){
  const hb=document.querySelector('.hb');
  if(!hb)return;
  const on=hb.classList.toggle('wide');
  btn.setAttribute('aria-expanded',on?'false':'true');
  btn.setAttribute('aria-label',on?'show the file tree':'hide the file tree');
  if(save!==false){try{localStorage.setItem('hbwide',on?'1':'')}catch(e){}}
}
function pulse(){
  if(matchMedia('(prefers-reduced-motion:reduce)').matches)return;
  const ks=[...document.querySelectorAll('.rail .keys .key')];
  ks.forEach((k,i)=>setTimeout(()=>{
    k.classList.remove('pulse');void k.offsetWidth;k.classList.add('pulse');
    setTimeout(()=>k.classList.remove('pulse'),460);
  },i*40));
}
function step(d){
  const i=TABNAMES.indexOf(document.querySelector('.key.on[data-t]')?.dataset.t);
  tab(TABNAMES[((i<0?0:i)+d+TABNAMES.length)%TABNAMES.length]);
}
function setGroup(btn,g){
  const c=btn.closest('.panel'); c.dataset.g=g;
  c.querySelectorAll('.keys.sm .key').forEach(b=>b.classList.toggle('on',b===btn));
  refilter(c);
}
function setProject(sel){const c=sel.closest('.panel');c.dataset.p=sel.value;refilter(c);}
const GKEY='hbgroups';
function shutList(){
  try{const a=JSON.parse(localStorage.getItem(GKEY));return Array.isArray(a)?a:[];}
  catch(e){return [];}
}
function setShut(b,shut,anim){
  const r=b.querySelector('.rows');
  b.classList.toggle('shut',shut);
  b.querySelector('.gtog').setAttribute('aria-expanded',shut?'false':'true');
  r.style.maxHeight='';
  if(!anim||matchMedia('(prefers-reduced-motion:reduce)').matches)return;
  const h=r.scrollHeight+'px';
  r.style.maxHeight=shut?h:'0px';
  void r.offsetHeight;
  r.style.maxHeight=shut?'0px':h;
  setTimeout(()=>{r.style.maxHeight='';},160);
}
function togGroup(btn){
  const b=btn.closest('.pblock'), shut=btn.getAttribute('aria-expanded')!=='false';
  setShut(b,shut,true);
  const list=shutList(), i=list.indexOf(b.dataset.p);
  if(shut&&i<0)list.push(b.dataset.p); else if(!shut&&i>=0)list.splice(i,1);
  try{localStorage.setItem(GKEY,JSON.stringify(list));}catch(e){}
}
function restoreGroups(root){
  const list=shutList();
  (root||document).querySelectorAll('.pblock').forEach(b=>{
    if(b.querySelector('.gtog'))setShut(b,list.indexOf(b.dataset.p)>=0,false);
  });
}
function refilter(c){
  const g=c.dataset.g||'all', p=c.dataset.p||'';
  let total=0;
  c.querySelectorAll('.pblock').forEach(b=>{
    let vis=0,open=0;
    b.querySelectorAll('.row').forEach(r=>{
      const ok=(g==='all'||(r.dataset.g||'').includes(g))&&(!p||b.dataset.p===p);
      r.hidden=!ok; if(ok){vis++;if(!r.classList.contains('done'))open++;}
    });
    b.hidden=!vis; total+=open;
    const n=b.querySelector('.group .n'); if(n) n.textContent=open;
  });
  const m=c.querySelector('.count'); if(m) m.textContent=total;
  const e=c.querySelector('.empty'); if(e) e.hidden=total>0;
}
function fmtLeft(ms){
  if(ms<0)return '0m';
  const m=Math.round(ms/60000), h=Math.floor(m/60);
  return h? h+'h '+String(m%60).padStart(2,'0')+'m' : m+'m';
}
function ticks(){
  document.querySelectorAll('[data-until]').forEach(el=>{
    el.textContent=fmtLeft(el.dataset.until*1000-Date.now());
  });
}
addEventListener('DOMContentLoaded',()=>{
  const h=(location.hash||'#'+ACTIVE).slice(1);
  tab(document.getElementById('t-'+h)?h:'today');
  ticks(); setInterval(ticks,30000);
  restoreWide(); restoreGroups(); live();
});

const SRCTABS={todo:['today','tasks'],questions:['questions','today'],
  capture:['inbox','today'],memory:['today'],brief:[],commits:[]};
let dueTabs=new Set(), pending=new Set(), flushTimer=null;
function live(){
  if(!window.EventSource)return;
  const es=new EventSource('/events');
  es.onmessage=e=>{
    if(document.hidden){pending.add(e.data);return;}
    mark(e.data);
  };
}
function mark(src){
  (SRCTABS[src]||[]).forEach(t=>dueTabs.add(t));
  dueTabs.add('rail');
  if(src==='commits'&&location.pathname==='/handbook')dueTabs.add('handbook');
  clearTimeout(flushTimer); flushTimer=setTimeout(flush,120);
}
addEventListener('visibilitychange',()=>{
  if(document.hidden||!pending.size)return;
  const p=[...pending]; pending.clear(); p.forEach(mark);
});
function flush(){
  const names=[...dueTabs]; dueTabs.clear();
  for(const name of names){
    if(name==='rail'){refreshRail();continue;}
    if(name==='handbook'){nav(location.pathname+location.search,false);continue;}
    const el=document.getElementById('t-'+name);
    if(!el)continue;
    const keep=[...el.querySelectorAll('.panel')].map(p=>[p.dataset.g,p.dataset.p]);
    fetch('/fragment?tab='+name).then(r=>r.text()).then(h=>{
      swapIn(el,h);
      el.querySelectorAll('.panel').forEach((p,i)=>{
        const [g,pr]=keep[i]||[];
        if(!g&&!pr)return;
        if(g)p.dataset.g=g;
        if(pr)p.dataset.p=pr;
        p.querySelectorAll('.keys.sm .key').forEach(b=>
          b.classList.toggle('on',(b.getAttribute('onclick')||'').includes("'"+g+"'")));
        const s=p.querySelector('select'); if(s&&pr)s.value=pr;
        refilter(p);
      });
      restoreGroups(el);
    });
  }
}
function refreshRail(){
  fetch('/fragment?tab=rail').then(r=>r.text()).then(h=>{
    const rail=document.querySelector('.rail');
    const tmp=document.createElement('div'); tmp.innerHTML=h;
    rail.querySelector('.runs').replaceWith(tmp.querySelector('.runs'));
    rail.querySelector('.keys').replaceWith(tmp.querySelector('.keys'));
    tab(document.querySelector('.tab.on')?.id.slice(2)||'today');
    ticks();restoreSort();
  });
}
addEventListener('keydown',e=>{
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  const a=document.activeElement;
  if(e.key==='Escape'&&a&&a.blur){a.blur();return;}
  if(a&&/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))return;
  if(a&&a.isContentEditable)return;
  const k=e.key.toLowerCase();
  if(k==='w'||k==='s'){e.preventDefault();step(k==='w'?-1:1);return;}
  const i=TABKEYS.indexOf(e.key);
  if(i>=0&&TABNAMES[i]){e.preventDefault();tab(TABNAMES[i],false);}
});
function swap(row,html){
  const prev=row.previousSibling, parent=row.parentNode;
  row.outerHTML=html;
  const fresh=prev?prev.nextSibling:parent.firstChild;
  if(fresh&&fresh.classList)fresh.classList.add('just');
}
function oops(f,row){
  const host=row||f;
  if(host.querySelector('.oops'))return;
  const line=document.createElement('div');
  line.className='oops'; line.textContent='That did not save. Try again.';
  (row?row.querySelector('.body')||row:f).appendChild(line);
  setTimeout(()=>line.remove(),4000);
}
document.addEventListener('submit',e=>{
  const f=e.target;
  if(!(f instanceof HTMLFormElement))return;
  const act=f.getAttribute('action');
  e.preventDefault();
  const row=f.closest('.row');
  const box=f.classList.contains('cb');
  const btn=f.querySelector('button');
  let revert=null;
  if(box&&row){
    const was=row.classList.contains('done');
    row.classList.toggle('done',!was);
    row.classList.toggle('just',!was);
    revert=()=>{row.classList.toggle('done',was);row.classList.remove('just');};
  }else if(btn){
    btn.disabled=true; btn.classList.add('pend');
    revert=()=>{btn.disabled=false;btn.classList.remove('pend');};
  }
  fetch(act,{method:'POST',headers:{'X-Row':'1'},
             body:new URLSearchParams(new FormData(f))})
    .then(r=>r.ok?r.text():Promise.reject(0))
    .then(t=>{
      if(row&&t.startsWith('<div class="row')){swap(row,t);return;}
      const q=f.closest('.q');
      if(q){q.innerHTML='<div class="saved">saved</div>';return;}
      const ta=f.querySelector('textarea');
      if(ta){ta.value='';f.insertAdjacentHTML('beforeend','<div class="saved">saved</div>');
        if(revert)revert();return;}
      if(row&&t){swap(row,t);return;}
      if(revert)revert();
    })
    .catch(()=>{if(revert)revert();oops(f,row);});
});

function graph(){
  const cv=document.getElementById('vgraph');
  if(!cv)return;
  const GDEBUG=location.search.includes('gdebug');
  const t0=performance.now();
  const tip=document.getElementById('gtip'), ctx=cv.getContext('2d');
  fetch('/handbook-graph').then(r=>r.json()).then(g=>{
    const N=g.nodes, ED=g.edges, n=N.length;
    if(!n)return;
    const CKEY='vg:'+g.hash;
    let saved=null;
    try{const raw=localStorage.getItem(CKEY);
      const a=raw&&JSON.parse(raw);
      if(a&&a.length===n*2)saved=a;}catch(e){}
    function keep(){
      try{
        for(let i=localStorage.length-1;i>=0;i--){
          const k=localStorage.key(i);
          if(k&&k.startsWith('vg:')&&k!==CKEY)localStorage.removeItem(k);
        }
        const a=new Array(n*2);
        for(let i=0;i<n;i++){a[i*2]=Math.round(x[i]*10)/10;
          a[i*2+1]=Math.round(y[i]*10)/10;}
        localStorage.setItem(CKEY,JSON.stringify(a));
      }catch(e){}
    }
    const x=new Float64Array(n), y=new Float64Array(n);
    const core=N.map((v,i)=>i);
    const S=600;
    core.forEach((i,k)=>{const a=k*2.39996, r=S*0.34*Math.sqrt((k+1)/core.length);
      x[i]=Math.cos(a)*r; y[i]=Math.sin(a)*r;});
    const K=S/Math.sqrt(core.length||1)*0.62, SPRING=3;
    const dx=new Float64Array(n), dy=new Float64Array(n);
    let temp=S*0.10;
    function relax(){
      dx.fill(0); dy.fill(0);
      for(let a=0;a<core.length;a++){
        const i=core[a];
        for(let b=a+1;b<core.length;b++){
          const j=core[b];
          let ux=x[i]-x[j], uy=y[i]-y[j], d=Math.hypot(ux,uy);
          if(d<0.01){ux=Math.random()-0.5; uy=Math.random()-0.5; d=0.01;}
          const f=K*K/d/d;
          dx[i]+=ux*f; dy[i]+=uy*f; dx[j]-=ux*f; dy[j]-=uy*f;
        }
      }
      for(const e of ED){
        const i=e[0], j=e[1];
        let ux=x[i]-x[j], uy=y[i]-y[j], d=Math.hypot(ux,uy);
        if(d<0.01)continue;
        const f=d/K*SPRING;
        dx[i]-=ux*f; dy[i]-=uy*f; dx[j]+=ux*f; dy[j]+=uy*f;
      }
      for(const i of core){
        const d=Math.hypot(dx[i],dy[i])||1, m=Math.min(d,temp)/d;
        x[i]+=dx[i]*m; y[i]+=dy[i]*m;
      }
      temp*=0.94;
    }
    if(saved){for(let i=0;i<n;i++){x[i]=saved[i*2];y[i]=saved[i*2+1];}}
    else{for(let k=0;k<90;k++)relax(); keep();}

    const rad=i=>3+Math.sqrt(N[i].links_count)*1.6;
    let MAXRAD=3;
    for(let i=0;i<n;i++)MAXRAD=Math.max(MAXRAD,rad(i));
    const adj=Array.from({length:n},()=>[]);
    ED.forEach((e,k)=>{adj[e[0]].push(k);adj[e[1]].push(k);});
    let edgePath=null;
    function buildEdges(skip){
      const p=new Path2D();
      for(const e of ED){
        if(skip!==undefined&&(e[0]===skip||e[1]===skip))continue;
        p.moveTo(x[e[0]],y[e[0]]);p.lineTo(x[e[1]],y[e[1]]);
      }
      return p;
    }
    edgePath=buildEdges();

    let G=null;
    function buildGrid(){
      let a=1e9,b=1e9,c=-1e9,d=-1e9;
      for(let i=0;i<n;i++){a=Math.min(a,x[i]);b=Math.min(b,y[i]);
        c=Math.max(c,x[i]);d=Math.max(d,y[i]);}
      const cols=Math.max(1,Math.ceil(Math.sqrt(n)));
      G={x0:a,y0:b,cw:(c-a)/cols||1,ch:(d-b)/cols||1,cols,
         cells:Array.from({length:cols*cols},()=>[])};
      for(let i=0;i<n;i++){
        const cx=Math.min(cols-1,Math.max(0,Math.floor((x[i]-G.x0)/G.cw)));
        const cy=Math.min(cols-1,Math.max(0,Math.floor((y[i]-G.y0)/G.ch)));
        G.cells[cy*cols+cx].push(i);
      }
    }
    buildGrid();

    let tx=0, ty=0, sc=1, hi=-1, dpr=1, cw=0, ch=0;
    let inkc='#888', accent='#FF4A00';
    function colors(){
      const s=getComputedStyle(cv);
      inkc=(s.getPropertyValue('--ink-2')||'#888').trim();
      accent=(s.getPropertyValue('--accent')||'#FF4A00').trim();
    }
    function fit(){
      let a=1e9,b=1e9,c=-1e9,d=-1e9;
      for(let i=0;i<n;i++){a=Math.min(a,x[i]);b=Math.min(b,y[i]);
        c=Math.max(c,x[i]);d=Math.max(d,y[i]);}
      sc=Math.min(cw/(c-a+70),ch/(d-b+70));
      tx=cw/2-(a+c)/2*sc; ty=ch/2-(b+d)/2*sc;
    }
    function size(){
      dpr=devicePixelRatio||1; cw=cv.clientWidth; ch=cv.clientHeight;
      cv.width=Math.round(cw*dpr); cv.height=Math.round(ch*dpr);
    }
    let hiCache=new Array(n);
    let dragNode=-1, restPath=null, livePath=null;
    function hiPath(i){
      if(hiCache[i])return hiCache[i];
      const p=new Path2D();
      for(const k of adj[i]){
        const j=ED[k][0]===i?ED[k][1]:ED[k][0];
        p.moveTo(x[i],y[i]); p.lineTo(x[j],y[j]);
      }
      return (hiCache[i]=p);
    }
    let hiP=null;
    function draw(){
      ctx.setTransform(dpr,0,0,dpr,0,0);
      ctx.clearRect(0,0,cw,ch);
      ctx.save(); ctx.translate(tx,ty); ctx.scale(sc,sc);
      ctx.lineWidth=1/sc; ctx.strokeStyle=inkc;
      ctx.globalAlpha=hi<0?.3:.08;
      ctx.stroke(dragNode>=0?restPath:edgePath);
      if(dragNode>=0&&livePath)ctx.stroke(livePath);
      if(hi>=0&&hiP){
        ctx.strokeStyle=accent; ctx.globalAlpha=.9;
        ctx.lineWidth=1.6/sc; ctx.stroke(hiP); ctx.lineWidth=1/sc;
      }
      const near=hi<0?null:new Set([hi]);
      if(near)for(const k of adj[hi])near.add(ED[k][0]===hi?ED[k][1]:ED[k][0]);
      for(let i=0;i<n;i++){
        ctx.fillStyle=GTONE[N[i].domain]||GTONE.root;
        ctx.globalAlpha=near&&!near.has(i)?.16:(N[i].links_count?1:.45);
        ctx.beginPath(); ctx.arc(x[i],y[i],rad(i)/sc,0,Math.PI*2); ctx.fill();
      }
      if(hi>=0){
        ctx.globalAlpha=1; ctx.strokeStyle=accent; ctx.lineWidth=1.6/sc;
        ctx.beginPath(); ctx.arc(x[hi],y[hi],(rad(hi)+3.5)/sc,0,Math.PI*2); ctx.stroke();
      }
      ctx.globalAlpha=1; ctx.restore();
    }
    let queued=false;
    function need(){
      if(queued)return;
      if(document.hidden){draw();return;}
      queued=true;
      requestAnimationFrame(()=>{queued=false;draw();});
    }
    let fitted=false;
    function relayout(){
      if(!cv.clientWidth||!cv.clientHeight)return;
      size();
      if(!fitted){fit();fitted=true;}
      draw();
    }
    colors(); relayout();
    if(GDEBUG)console.log('graph tti',Math.round(performance.now()-t0)+'ms',
      saved?'cached':'laid out',n+' nodes',ED.length+' edges');
    new ResizeObserver(relayout).observe(cv);
    const dm=matchMedia('(prefers-color-scheme:dark)');
    (dm.addEventListener?dm.addEventListener.bind(dm,'change'):dm.addListener.bind(dm))
      (()=>{colors();need();});

    function near(ev){
      const r=cv.getBoundingClientRect();
      const px=(ev.clientX-r.left-tx)/sc, py=(ev.clientY-r.top-ty)/sc;
      const reach=(MAXRAD+7)/sc;
      const rr=Math.min(G.cols,Math.ceil(reach/Math.min(G.cw,G.ch))+1);
      const cx=Math.floor((px-G.x0)/G.cw), cy=Math.floor((py-G.y0)/G.ch);
      let best=-1, bd=1e9;
      for(let j=cy-rr;j<=cy+rr;j++){
        if(j<0||j>=G.cols)continue;
        for(let i=cx-rr;i<=cx+rr;i++){
          if(i<0||i>=G.cols)continue;
          for(const k of G.cells[j*G.cols+i]){
            const d=(x[k]-px)**2+(y[k]-py)**2;
            if(d<bd){bd=d;best=k;}
          }
        }
      }
      return best>=0&&bd<Math.pow((rad(best)+7)/sc,2)?best:-1;
    }
    function liveEdges(i){
      const p=new Path2D();
      for(const k of adj[i]){
        const j=ED[k][0]===i?ED[k][1]:ED[k][0];
        p.moveTo(x[i],y[i]); p.lineTo(x[j],y[j]);
      }
      return p;
    }
    let drag=null;
    cv.addEventListener('pointerdown',e=>{
      drag={x:e.clientX,y:e.clientY,tx,ty,m:0,node:near(e)};
      cv.setPointerCapture(e.pointerId);
    });
    cv.addEventListener('pointerup',e=>{
      const was=drag, moved=drag&&drag.m>4;
      drag=null;
      if(dragNode>=0){
        hiCache=new Array(n); hiP=hi>=0?hiPath(hi):null;
        edgePath=buildEdges(); buildGrid(); keep();
        dragNode=-1; restPath=livePath=null; need();
        return;
      }
      if(moved||!was)return;
      const i=near(e);
      if(i>=0)nav('/handbook?page='+encodeURIComponent(N[i].path));
    });
    cv.addEventListener('pointermove',e=>{
      if(drag&&drag.node>=0){
        drag.m+=Math.abs(e.clientX-drag.x)+Math.abs(e.clientY-drag.y);
        if(drag.m<=4)return;
        if(dragNode<0){dragNode=drag.node; restPath=buildEdges(dragNode);
          hi=dragNode; hiP=null; tip.style.display='none';}
        const r=cv.getBoundingClientRect();
        x[dragNode]=(e.clientX-r.left-tx)/sc; y[dragNode]=(e.clientY-r.top-ty)/sc;
        livePath=liveEdges(dragNode); need(); return;
      }
      if(drag){drag.m+=Math.abs(e.clientX-drag.x)+Math.abs(e.clientY-drag.y);
        tx=drag.tx+e.clientX-drag.x; ty=drag.ty+e.clientY-drag.y; need(); return;}
      const i=near(e), r=cv.getBoundingClientRect();
      if(i!==hi){hi=i; hiP=i>=0?hiPath(i):null; need();}
      if(i<0){tip.style.display='none';return;}
      tip.textContent=N[i].id;
      tip.style.display='block';
      tip.style.left=(e.clientX-r.left+12)+'px';
      tip.style.top=(e.clientY-r.top+12)+'px';
    });
    cv.addEventListener('pointerleave',()=>{
      if(drag)return;
      tip.style.display='none';
      if(hi>=0){hi=-1;hiP=null;need();}
    });
    cv.addEventListener('wheel',e=>{
      e.preventDefault();
      const r=cv.getBoundingClientRect(), mx=e.clientX-r.left, my=e.clientY-r.top;
      const f=Math.exp(-e.deltaY*0.0015), s2=Math.min(6,Math.max(0.15,sc*f));
      tx=mx-(mx-tx)*(s2/sc); ty=my-(my-ty)*(s2/sc); sc=s2; need();
    },{passive:false});
  }).catch(()=>{cv.parentNode.innerHTML='<p class="empty" style="padding:16px">'
    +'The graph could not be built.</p>';});
}
addEventListener('DOMContentLoaded',graph);
"""


def greeting():
    h = time.localtime().tm_hour
    part = "morning" if h < 12 else ("afternoon" if h < 19 else "evening")
    who = CONFIG["owner"]
    return f"Good {part}, {who}." if who else f"Good {part}."


def panel(label, body, count="", head_extra="", cls="", cap=""):
    if not body:
        return ""
    meta = f'<span class="count">{E(str(count))}</span>' if count != "" else ""
    right = f'<div class="hctl">{head_extra}{meta}</div>' if head_extra or meta else ""
    caption = f'<div class="pcap">{E(cap)}</div>' if cap else ""
    return (f'<section class="panel {cls}"><div class="phead"><h2>{E(label)}</h2>'
            f'{right}</div>{caption}{body}</section>')


def out_link(href, label):
    return (f'<a class="out" href="{E(href)}" target="_blank" '
            f'rel="noopener">{E(label)}</a>')


def stat(label, value, caption="", warn=False, until=None):
    cap = f'<div class="c">{E(caption)}</div>' if caption else ""
    val = (f'<div class="v" data-until="{int(until)}">{E(str(value))}</div>' if until
           else f'<div class="v">{E(str(value))}</div>')
    return (f'<div class="stat{" warn" if warn else ""}"><div class="lab">{E(label)}</div>'
            f'{val}{cap}</div>')


def hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{E(str(value))}">'


def icon(name, title=""):
    if name in ASSET_ICONS:
        return (f'<img class="ic tile" src="/assets/{ASSET_ICONS[name]}" '
                f'alt="{E(title)}" title="{E(title)}">')
    if name not in ICONS:
        return ""
    if not title:
        return f'<svg class="ic" aria-hidden="true"><use href="#i-{name}"/></svg>'
    return (f'<svg class="ic" role="img" aria-label="{E(title)}">'
            f'<title>{E(title)}</title><use href="#i-{name}"/></svg>')


def git_mark(state):
    if state == "deployed":
        return '<span class="live" title="deployed">live</span>'
    if state not in GIT_MARKS:
        return ""
    return (f'<svg class="gm {state}" role="img" aria-label="{state}">'
            f'<title>{state}</title><use href="#g-{state}"/></svg>')


def source_icon(t):
    for prefix, name in CONFIG["icon_projects"].items():
        if t["project"].startswith(prefix):
            return name
    s = t["source"].lower()
    if "dashboard" in s:
        return "dashboard"
    if "mail" in s or "correo" in s:
        return "mail"
    if s.startswith("from ") or " from " in s:
        return "granola"
    if "added" in s:
        return "claude"
    return ""


TICK = ('<svg class="tick" viewBox="0 0 18 18" aria-hidden="true">'
        '<path d="M4.5 9.2 7.6 12.3 13.5 5.9"/></svg>')


def task_check(t, tab):
    action = "reopen" if t["done"] else "done"
    label = "Reopen this task" if t["done"] else "Close this task"
    title = ("put it back on the board" if t["done"]
             else "check it off, it is done")
    return (f'<form class="cb" method="post" action="/task">'
            f'{hidden("id", t["id"])}{hidden("action", action)}{hidden("tab", tab)}'
            f'<button class="box" title="{title}" aria-label="{label}">{TICK}'
            f'</button></form>')


def undo_form(t, tab, action, label, n=0):
    return (f'<form method="post" action="/task">{hidden("id", t["id"])}'
            f'{hidden("action", action)}{hidden("n", n)}{hidden("tab", tab)}'
            f'<button class="undo">{E(label)}</button></form>')


def row_state(t, tab):
    bits = []
    if t["not_needed"]:
        bits.append(("closed as not needed", undo_form(t, tab, "reopen", "undo")))
    elif t["closed_here"]:
        bits.append(("closed from here", undo_form(t, tab, "reopen", "undo")))
    elif t["reopened"]:
        bits.append(("reopened from here", undo_form(t, tab, "done", "undo")))
    elif t["done"]:
        bits.append(("done", ""))
    if t["reframed"]:
        bits.append(("reframe asked", undo_form(t, tab, "unreframe", "undo")))
    for f in t["flags"]:
        if f["unsure"]:
            bits.append(("flagged unsure", undo_form(t, tab, "unflag", "undo", f["n"])))
        else:
            bits.append((f["text"],
                         undo_form(t, tab, "unflag", "remove flag", f["n"])))
    if not bits:
        return ""
    return "".join(f'<div class="state"><span class="w">{E(text)}</span>{ctl}</div>'
                   for text, ctl in bits)


def acts_button(t):
    return (f'<button type="button" class="cmt" onclick="toggleReframe(\'{t["id"]}\')" '
            f'title="tell the next sync what is wrong with this line">comment</button>'
            '<button type="button" class="mk" onclick="toggleActs(this)" '
            'aria-expanded="false" aria-label="more actions for this task" '
            'title="more actions">more</button>')


def task_actions(t, tab):
    return (f'<div class="acts"><div class="acts-in">'
            f'<form method="post" action="/task">'
            f'{hidden("id", t["id"])}{hidden("action", "notneeded")}{hidden("tab", tab)}'
            f'<button class="btn" title="close it, it should never have been a task">'
            f'not needed</button></form>'
            f'<button type="button" class="btn" title="tell the next sync what is wrong '
            f'with this line; it rewrites it" '
            f'onclick="toggleReframe(\'{t["id"]}\')">comment</button>'
            f'<button type="button" class="btn" title="change the title or the note yourself" '
            f'onclick="toggleEdit(\'{t["id"]}\')">edit</button></div>'
            f'<div class="reframe" id="ed-{t["id"]}">'
            f'<form method="post" action="/edit-task">'
            f'{hidden("id", t["id"])}{hidden("tab", tab)}'
            f'<input type="text" name="title" maxlength="60" value="{E(t["title"][:60])}" placeholder="Title">'
            f'<input type="text" name="note" value="{E(t["rest"].splitlines()[0] if t["rest"] else "")}" placeholder="One line of context">'
            f'<button class="btn pri">save</button></form></div>'
            f'<div class="reframe" id="rf-{t["id"]}">'
            f'<form method="post" action="/reframe">'
            f'{hidden("id", t["id"])}{hidden("title", t["title"][:70])}{hidden("tab", tab)}'
            f'<input type="text" name="comment" placeholder="What is wrong, or what should it say?">'
            f'<button class="btn pri">send</button></form></div></div>')


TAGGED = re.compile(r"^[^\s·][^·\n]{0,23} · ")


def strip_tag(title, group):
    """Drop a leading "{tag} · " when the tag is the group the row sits in."""
    for tag in (group, short_project(group)):
        head = f"{tag} · "
        if tag and title[:len(head)].casefold() == head.casefold():
            return title[len(head):]
    return title


def pfx_span(prefix, title):
    if not prefix or TAGGED.match(title):
        return ""
    return f'<span class="pfx">{E(prefix)} · </span>'


def task_row(t, tab, groups="all", why="", note="", note_title="", prefix="",
             subcls="", group=""):
    ico = source_icon(t)
    age = f'{t["age"]}d' if t["age"] is not None else ""
    label = note or age
    meta = ((icon(ico, t["source"]) if ico else "")
            + (f'<span title="{E(note_title or t["source"])}">{E(label)}</span>'
               if label else ""))
    lines = [l for l in t["rest"].splitlines() if l.strip()]
    more = (f'<details class="more"><summary data-n="{len(lines)}"></summary>'
            f'<ul class="mlines">'
            + "".join(f"<li>{E(l.strip())}</li>" for l in lines)
            + "</ul></details>") if lines else ""
    sub = f'<div class="sub {subcls}">{E(why)}</div>' if why else ""
    title = strip_tag(t["title"], group)
    pfx = pfx_span(prefix, title)
    return (f'<div class="row{" done" if t["done"] else ""}" data-g="{groups}" '
            f'data-age="{t["age"] if t["age"] is not None else ""}">'
            f'{task_check(t, tab)}<div class="body">'
            f'<div class="line"><span class="t">{pfx}{E(title)}</span>'
            f'<span class="meta-r">{meta}</span>{acts_button(t)}</div>'
            f'{sub}{task_actions(t, tab)}{row_state(t, tab)}{more}</div></div>')


def find_task(tid):
    for s in todo_sections():
        for t in s["items"]:
            if t["id"] == tid:
                return t
    return None


# ------------------------------------------------------------------ modules
# A module takes the context dict and returns HTML, or "" to render nothing.

def m_anchor(c):
    return (f'<section class="panel anchor">'
            f'<div class="day"><h1>{time.strftime("%A %-d %B")}</h1>'
            f'<p>{E(greeting())}</p></div>{extra("anchor")}</section>')


def drift():
    if not CONFIG["lint"]:
        return None
    sys.path.insert(0, str(_HERE))
    import lint
    try:
        return lint.run(VAULT, CONFIG["lint"])
    except Exception as exc:
        return [str(exc)[:120]]


def drift_stat(c):
    found = c.get("drift")
    if found is None:
        return ""
    first = found[0].split(": ", 1)[-1] if found else "rules and files agree"
    return stat("drift", len(found), first[:60], warn=bool(found))


def m_stats(c):
    body = ('<div class="stats">'
            + stat("open tasks", work_count(c), "board and linear" if on("linear") else "on the board")
            + stat("questions", c["q_total"], "waiting for your answer")
            + stat("deals in play", len(c["deals"]) - c["closed"], "from Attio")
            + stat("waiting", len(c["inbox"]), "inbox notes not yet picked up",
                   warn=bool(c["inbox"]))
            + drift_stat(c)
            + "</div>")
    return f'<section class="panel">{body}</section>'


def extra(slot):
    """HTML from <vault>/.dashboard/extras.py, a private module with one function per slot.

    Slots today: anchor(config). Missing module or slot renders nothing."""
    mod = _cache.get("extras")
    if mod is None:
        path = VAULT / ".dashboard" / "extras.py"
        mod = False
        if path.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location("handbook_extras", path)
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
            except Exception:
                mod = False
        _cache["extras"] = mod
    fn = getattr(mod, slot, None) if mod else None
    try:
        return fn(CONFIG) if fn else ""
    except Exception as exc:
        return f'<p class="empty">{E(slot)} extra failed: {E(str(exc)[:80])}</p>'


ORDINALS = ("", "first", "second", "third", "fourth", "fifth", "sixth", "seventh")


def day_title(n):
    word = ORDINALS[n] if 0 < n < len(ORDINALS) else f"{n}th"
    return f"{word} day as a tier-1 pick"


def short_project(name):
    return re.split(r"\s[—–-]\s|[:,]", name)[0].strip()[:22]


def bench_prefix(text, t):
    if t:
        return short_project(t["project"])
    head = text.split(":", 1)[0]
    return head if 0 < len(head) <= 20 and head != text else ""


def bench_row(text, c, tab="today"):
    t = match_task(text, c["all_items"])
    prefix = bench_prefix(text, t)
    if t:
        return task_row(t, tab, "all", "", "", "", prefix)
    pfx = pfx_span(prefix, text)
    return (f'<div class="row" title="No single task on the board matches this line, '
            f'so it stays read only."><span class="gap"></span>'
            f'<div class="body"><div class="line">'
            f'<span class="t ro">{pfx}{E(text)}</span>'
            f'<span class="meta-r"><span title="the morning run wrote this pick, '
            f'but no line on the board matches it">not on the board</span></span>'
            f'<span class="mkgap"></span>'
            f'</div></div></div>')


def promoted_row():
    """Refill the focus strip from the bench, display only.

    `Assistant-Memory.md` is owned by the scheduled runs and never written here."""
    picks, bench = focus_picks()
    taken = [p["text"].lower() for p in picks]
    left = [b for b in bench
            if not any(b.lower() in p or p in b.lower() for p in taken)]
    if not left:
        return ""
    board = [t for s in todo_sections() for t in s["items"]
             if not s["title"].startswith("Clocks") and s["title"] != "Done"]
    return bench_row(left[0], {"all_items": board})


def on_deck(c, used, want):
    """Board items to top up Next up: a clock's project first, then oldest sitting."""
    if want <= 0:
        return []
    tokens = [t for t in (re.split(r"[/\s]", r["project"])[0].lower()
                          for r in c["clocks"]) if t]
    ranked = []
    for t in c["items"]:
        if t["id"] in used:
            continue
        blob = (t["project"] + " " + t["title"] + " " + t["rest"]).lower()
        clocked = any(tok in blob for tok in tokens)
        ranked.append((0 if clocked else 1, -(t["age"] or 0), t,
                       "on deck · has a clock" if clocked
                       else "on deck · oldest sitting"))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [(t, why) for _, _, t, why in ranked[:want]]


def linear_next(c, n=5):
    """The first n assigned Linear issues in plan order, when Linear is on."""
    if not on("linear") or not secret("LINEAR_API_KEY", CONFIG["linear_key_path"]):
        return []
    got = cached("linear", 60, linear_issues)
    return linear_ordered(got)[:n] if isinstance(got, list) else []


def m_focus(c):
    picks, bench = c["focus"]
    if not picks and not bench:
        nxt = linear_next(c)
        if not nxt:
            return ""
        rows = "".join(
            f'<div class="row"><span class="gap"></span><div class="body"><div class="line">'
            f'<a class="t" href="{E(x.get("url", ""))}" target="_blank" rel="noopener">'
            f'<span class="pfx">{E(x.get("identifier", ""))} · </span>{E(x.get("title", ""))}</a>'
            f'<span class="pill tag">{E((x.get("project") or {}).get("name") or "")}</span>{who_pill(x)}'
            f'</div></div></div>' for x in nxt)
        return panel("next up", f'<div class="rows">{rows}</div>', len(nxt),
                     cap="your Linear tickets, in plan order")
    rows, used = "", set()
    for p in picks:
        t = match_task(p["text"], c["all_items"])
        day = f'day {p["day"]}' if p["day"] else ""
        if t:
            used.add(t["id"])
            rows += task_row(t, "today", "all", p["why"], day,
                             day_title(p["day"]) if p["day"] else "")
        else:
            why = f'<div class="sub">{E(p["why"])}</div>' if p["why"] else ""
            title = day_title(p["day"]) if p["day"] else ""
            rows += (f'<div class="row" title="No single task on the board matches '
                     f'this pick, so it stays read only.">'
                     f'<span class="gap"></span><div class="body"><div class="line">'
                     f'<span class="t ro">{E(p["text"])}</span>'
                     f'<span class="meta-r"><span title="the morning run wrote this pick, '
                     f'but no line on the board matches it">not on the board</span>'
                     f'<span title="{E(title)}">{E(day)}</span>'
                     f'</span><span class="mkgap"></span>'
                     f'</div>{why}</div></div>')
    body = f'<div class="rows">{rows}</div>'
    bench = bench[:BENCH_MAX]
    next_rows = ""
    for b in bench:
        t = match_task(b, c["all_items"])
        if t:
            used.add(t["id"])
        next_rows += bench_row(b, c)
    deck = on_deck(c, used, NEXT_UP - len(bench))
    for t, why in deck:
        next_rows += task_row(t, "today", "all", why, "", "",
                              short_project(t["project"]), "dk")
    if next_rows:
        body += ('<div class="group">Next up<span class="n">'
                 f'{len(bench) + len(deck)}</span></div><div class="rows next">'
                 + next_rows + "</div>")
    return panel("focus", body, len(picks), cap="today's picks, oldest first")


def m_clocks(c):
    if not c["clocks"]:
        return ""
    cells = ""
    for r in c["clocks"]:
        left, due = r["left"], r["due"]
        if left is None:
            value, warn = "no date", False
        elif left < 0:
            value, warn = f"passed {abs(left)}d", True
        else:
            value, warn = due.strftime("%a %-d").lower(), left <= 2
        caption = " · ".join(x for x in (r["what"], r["project"]) if x)
        resolve = (f'<form class="cres" method="post" action="/clock">'
                   f'{hidden("id", r["id"])}{hidden("tab", "today")}'
                   f'<button class="undo" title="it happened, take it off the clock">'
                   f'mark resolved</button></form>')
        cell = stat("due", value, caption, warn)
        cells += cell[:-len("</div>")] + resolve + "</div>"
    return panel("clocks", f'<div class="stats">{cells}</div>', len(c["clocks"]))


def pblock(name, rows, n, sort=True, shut=False, extra=""):
    sort_sel = ('<select class="gsort" onchange="setSort(this)" onclick="event.stopPropagation()" '
                'title="order inside this group"><option value="board">board order</option>'
                '<option value="new">newest first</option><option value="old">oldest first</option></select>'
                if sort else "")
    return (f'<div class="pblock{" done shut" if shut else ""}" data-p="{E(name)}">'
            f'<button type="button" class="group gtog" aria-expanded="{"false" if shut else "true"}"'
            f' title="collapse or expand this group" onclick="togGroup(this)">{E(name)}'
            f'<span class="n">{n}</span>{extra}{sort_sel}'
            f'<span class="chev" aria-hidden="true"></span></button>'
            f'<div class="rows">{rows}</div></div>')


def m_work(c):
    """One list: Linear teams in plan order, board lines folded into the team of the same name,
    the rest of the board after, then Done."""
    teams, note = linear_blocks()
    board = {}
    for s in c["sections"]:
        if s["title"].startswith("Clocks") or s["title"] == "Done":
            continue
        opened = [t for t in s["items"] if not t["done"]]
        if opened:
            board[s["title"]] = opened
    done = [(s["title"], t) for s in c["sections"] for t in s["items"] if t["done"]]
    names = [x["title"] for x in c["sections"] if not x["title"].startswith("Clocks") and x["title"] != "Done"] \
        or list(CONFIG.get("lint", {}).get("sections", [])) or ["Inbox"]
    total = sum(n for _, _, n in teams) + sum(len(v) for v in board.values())
    if not total:
        hint = f'<p class="empty">{E(note)}</p>' if note else '<p class="empty">Nothing open.</p>'
        return panel("work", hint + add_form(names))

    def board_rows(title, items):
        out = ""
        for t in items:
            groups = ["all"]
            if t["age"] is not None and t["age"] >= AGING_DAYS:
                groups.append("aging")
            if t["age"] is not None and t["age"] <= RECENT_DAYS:
                groups.append("recent")
            out += task_row(t, "tasks", " ".join(groups), group=title)
        return out

    blocks, shown = "", []
    for team, rows, n in teams:
        match = next((k for k in board if k.lower() == team.lower()), None)
        extra = board_rows(match, board.pop(match)) if match else ""
        blocks += pblock(team, rows + extra, n + (extra.count('class="row"')), sort=False)
        shown.append(team)
    for title, items in board.items():
        blocks += pblock(title, board_rows(title, items), len(items))
        shown.append(title)
    if done:
        arch = (f'<form method="post" action="/archive" class="inl" onclick="event.stopPropagation()">'
                f'{hidden("tab", "tasks")}<button class="btn" title="move every done line to the archive file">'
                f'archive all</button></form>')
        rows = "".join(task_row(t, "tasks", "all", prefix=short_project(title), group=title) for title, t in done)
        blocks += pblock("Done", rows, len(done), sort=False, shut=True, extra=arch)
    bar = ('<div class="bar"><div class="keys row sm">'
           '<button class="key on" title="everything open" '
           'onclick="setGroup(this,\'all\')">all</button>'
           f'<button class="key" title="board lines untouched for {AGING_DAYS} days or more, '
           'the Friday review asks about these" '
           'onclick="setGroup(this,\'aging\')">sitting 14d+</button>'
           f'<button class="key" title="board lines written or touched in the last {RECENT_DAYS} days" '
           'onclick="setGroup(this,\'recent\')">new this week</button></div>'
           '<div class="pills"><button class="pill on" data-p="" onclick="setPill(this)">everything</button>'
           + "".join(f'<button class="pill" data-p="{E(n)}" onclick="setPill(this)">{E(n)}</button>'
                     for n in shown)
           + "</div></div>")
    hint = f'<p class="empty">{E(note)}</p>' if note else ""
    body = hint + bar + add_form(names) + blocks + '<p class="empty" hidden>Nothing in this view.</p>'
    return panel("work", body, total, cap="linear in plan order" if teams else "")


def add_form(names):
    opts = "".join(f'<option value="{E(n)}">{E(n)}</option>' for n in names)
    return ('<div class="addwrap"><button type="button" class="btn" onclick="toggleAdd()">add a line</button>'
            '<div class="reframe" id="addtask"><form method="post" action="/add-task">'
            + hidden("tab", "tasks")
            + f'<select name="section">{opts}</select>'
            '<input type="text" name="title" maxlength="60" placeholder="Title, verb first" required>'
            '<input type="text" name="note" placeholder="One line of context">'
            '<button class="btn pri">add</button></form></div></div>')


def m_build(c):
    b = c["build"]
    head = out_link(CONFIG["github_url"], "github")
    if not on("github"):
        return ""
    if "error" in b:
        return panel("pull requests", f'<p class="empty">{E(b["error"])}</p>', "", head)
    shipped = f'<span class="prov" title="pull requests merged in the last seven days">shipped this week: {b.get("shipped", 0)}</span>'
    if not b["review"] and not b["mine"]:
        return panel("pull requests", '<p class="empty">Nothing waiting on you.</p>',
                     "", head + shipped)

    def rows(items):
        return '<div class="rows">' + "".join(
            f'<div class="row">{git_mark(x.get("state", ""))}'
            f'<div class="body"><div class="line">'
            f'<a class="t" href="{E(x["url"])}" target="_blank" rel="noopener">'
            f'{E(x["repo"])} · {E(x["title"])}</a>'
            + (f'<span class="pill tag {"ok" if x["ci"] == "green" else ("warn" if x["ci"] == "red" else "")}">'
               f'{"ci green" if x["ci"] == "green" else ("ci red" if x["ci"] == "red" else "ci running")}</span>' if x.get("ci") else "")
            + f'<span class="prov">{E(x["age"])}</span></div></div></div>'
            for x in items) + "</div>"
    body = ""
    if b["review"]:
        body += f'<div class="group">Waiting for your review</div>{rows(b["review"])}'
    if b["mine"]:
        body += f'<div class="group">Yours, still open</div>{rows(b["mine"])}'
    return panel("pull requests", body, len(b["review"]) + len(b["mine"]), head + shipped)


def linear_row(x, first=None, triage=False):
    nxt = '<span class="pill tag ok">next</span>' if first and x.get("identifier") == first else ""
    tri = ('<span class="pill tag warn" title="filed by the meeting run, waiting for a founder to accept it">'
           'triage</span>' if triage else who_pill(x))
    proj = (x.get("project") or {}).get("name") or ""
    state = "" if triage else (x.get("state") or {}).get("name", "")
    prov = " · ".join(v for v in (proj, state) if v)
    return (f'<div class="row" data-g="all"><div class="body"><div class="line">'
            f'<a class="t" href="{E(x.get("url", ""))}" target="_blank" rel="noopener">'
            f'<span class="pfx">{E(x.get("identifier", ""))} · </span>{E(x.get("title", ""))}</a>'
            f'{nxt}{tri}<span class="prov">{E(prov)}</span></div></div></div>')


def linear_blocks():
    """(teams, note): teams = [(name, rows_html, n)] in plan order, triage rows last inside their team."""
    if not on("linear"):
        return [], ""
    if not secret("LINEAR_API_KEY", CONFIG["linear_key_path"]):
        return [], ("Linear is switched on but no key was found. Put a personal API key in "
                    "~/.secrets/linear-api-key or the LINEAR_API_KEY variable, then refresh.")
    got = cached("linear", 60, linear_issues)
    if isinstance(got, dict):
        return [], got["error"]
    ordered = linear_ordered(got)
    first = ordered[0].get("identifier") if ordered else None
    teams, rows = [], {}

    def team_of(x):
        # ponytail: the client is the project's first word ("Costco / Lockton" -> Costco); the team when there is no project
        proj = (x.get("project") or {}).get("name") or ""
        return proj.split()[0] if proj else (x.get("team") or {}).get("name") or "Linear"
    for x in ordered:
        rows.setdefault(team_of(x), []).append(linear_row(x, first))
    for x in linear_triage(got):
        rows.setdefault(team_of(x), []).append(linear_row(x, triage=True))
    return [(t, "".join(r), len(r)) for t, r in rows.items()], ""


def m_linear(c):
    """Linear as a fragment: one block per team in plan order, triage after. Used inside the work list."""
    teams, note = linear_blocks()
    if note:
        return f'<p class="empty">{E(note)}</p>'
    return "".join(f'<div class="group">{E(t)}</div><div class="rows">{rows}</div>' for t, rows, _ in teams)


ATTIO_TOP = 5


def m_attio(c):
    head = out_link(CONFIG["attio_url"], "open attio")
    if isinstance(c["deals_raw"], dict):
        return panel("attio", f'<p class="empty">Attio did not answer: '
                              f'{E(c["deals_raw"]["error"])}</p>', "", head)
    deals = c["deals"][:ATTIO_TOP]
    if not deals:
        return panel("attio", '<p class="empty">No deals yet.</p>', "", head)
    rows = "".join(
        f'<div class="row"><div class="body"><div class="line">'
        f'<span class="t mono">{E(d["name"])}</span>'
        f'<span class="prov">{E(d["stage"])}</span></div></div></div>'
        for d in deals)
    return panel("attio", f'<div class="rows">{rows}</div>',
                 len(c["deals"]), head)


PANEL_MODULES = {"build": m_build, "attio": m_attio}


def m_board(c):
    side = "".join(PANEL_MODULES[p["name"]](c)
                   for p in sorted(CONFIG["panels"], key=lambda x: x["order"])
                   if p["enabled"] and p["name"] in PANEL_MODULES)
    return (f'<div class="two"><div>{m_work(c)}</div>'
            f'<aside class="side">{side}</aside></div>')


STAGE_ORDER = ("Lead", "In Progress", "Won")


def stage_rank(name):
    return (STAGE_ORDER.index(name), "") if name in STAGE_ORDER else (99, name)


def deal_notes(name, company=""):
    """The Deal.md of a mounted Clients folder whose name or Matches line fits this deal."""
    root = hb_roots().get("Clients")
    if not root:
        return ""
    low = (company + " " + name).lower()
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        deal = d / "Deal.md"
        keys = [d.name.lower()]
        if deal.exists():
            m = re.search(r"^Matches:\s*(.+)$", deal.read_text(), re.M)
            if m:
                keys += [k.strip().lower() for k in m.group(1).split(",")]
        if any(k and k in low for k in keys) and deal.exists():
            return f"/handbook?page=@Clients/{urllib.parse.quote(d.name)}/Deal.md"
    return ""


def deal_card(d, step):
    bits = ""
    if d["value"]:
        bits += f'<span class="mono">{E(d["value"])}</span>'
    if d["days"] is not None:
        bits += (f'<span class="age" title="days since the stage last changed">'
                 f'{d["days"]}d</span>')
    meta = f'<span class="cm">{bits}</span>' if bits else ""
    stage = d.get("stage", "")
    tone = "ok" if stage.startswith("Won") else ("warn" if stage.startswith("Lost") else "")
    tag = f'<span class="pill tag {tone}">{E(stage)}</span>' if stage else ""
    if step:
        sub = f'<span class="nx">{E("Next: " + step)}</span>'
    elif stage.startswith(("Won", "Lost")):
        sub = ""
    else:
        sub = '<span class="nx"><span class="pill tag warn">no next step</span></span>'
    notes = deal_notes(d["name"], d.get("company", ""))
    note_link = f'<a class="out" href="{notes}">notes</a>' if notes else ""
    label = (f'<b>{E(d["company"])}</b> · {E(d["name"])}' if d.get("company") else E(d["name"]))
    nm = (f'<a class="nm" href="{E(d["url"])}" target="_blank" rel="noopener" '
          f'title="open the record in Attio">{label}</a>' if d["url"]
          else f'<span class="nm">{label}</span>')
    lost = " lost" if stage.startswith("Lost") else ""
    return f'<div class="card{lost}">{tag}{nm}{note_link}{meta}{sub}</div>'


def deals_total(deals):
    """Sum of the live deals' values when they share one currency, else ""."""
    nums, curs = [], set()
    for d in deals:
        if not d["value"] or d["stage"].startswith(("Won", "Lost")):
            continue
        m = re.match(r"([\d,]+)\s*(\w*)", d["value"])
        if not m:
            continue
        nums.append(int(m.group(1).replace(",", "")))
        curs.add(m.group(2))
    if not nums or len(curs) != 1:
        return ""
    return f"{sum(nums):,} {curs.pop()}".strip()


def m_pipeline(c):
    head = out_link(CONFIG["attio_url"], "attio")
    deals = c["deals"]
    if isinstance(c["deals_raw"], dict):
        return panel("pipeline", f'<p class="empty">Attio did not answer: '
                                 f'{E(c["deals_raw"]["error"])}</p>', "", head,
                     cap="from Attio")
    if not deals:
        return panel("pipeline", '<p class="empty">No deals yet.</p>', "", head,
                     cap="from Attio")
    steps = c["steps"]
    live = [d for d in deals if not d["stage"].startswith(("Won", "Lost"))]
    lost = [d for d in deals if d["stage"].startswith("Lost")]
    ordered = sorted(deals, key=lambda d: (stage_rank(d["stage"]), -(d["days"] or 0)))
    rows = "".join(deal_card(d, steps.get(d["id"]) or steps.get(d.get("company_id", ""))) for d in ordered)
    oldest = max((d["days"] or 0) for d in live) if live else 0
    total = deals_total(deals)
    totals = ('<div class="totals">'
              + f'<div class="tot"><div class="lab">in play</div><div class="v">{len(live)}</div></div>'
              + (f'<div class="tot"><div class="lab">value</div><div class="v">{E(total)}</div></div>' if total else "")
              + f'<div class="tot"><div class="lab">oldest</div><div class="v">{oldest}d</div></div>'
              + f'<div class="tot"><div class="lab">won</div><div class="v">{c["won"]}</div></div>'
              + "</div>")
    ctl = (f'<button type="button" class="pill" onclick="togLost(this)">show lost ({len(lost)})</button>'
           if lost else "")
    return panel("pipeline", f'{totals}<div class="rows deals">{rows}</div>',
                 len(deals), head + ctl, cap="from Attio")


def m_queue(c):
    if not c["queue"]:
        return ""
    rows = ""
    for q in c["queue"]:
        rows += (f'<details class="q"><summary><div class="qhead">'
                 f'<span class="lab">{E(q["id"])}</span>'
                 f'<h3>{E(q["title"])}</h3>'
                 f'<span class="prov">{E(q["asked"])}</span></div>'
                 f'<div class="qfirst">{E(q["first"])}</div></summary>'
                 f'<div class="qbody">{md_block(q["body"])}</div>'
                 f'<form method="post" action="/answer">'
                 f'{hidden("qid", q["id"])}{hidden("tab", "questions")}'
                 f'<textarea name="answer" rows="3" '
                 f'placeholder="Answer. Dictation welcome."></textarea>'
                 f'<div class="form-act"><button class="btn pri">answer</button></div>'
                 f'</form></details>')
    return panel("queue", rows, len(c["queue"]))


def m_intake(c):
    out = ""
    for f in intake_files():
        qs = [q for q in intake_questions(f) if not q["answered"]]
        if not qs:
            continue
        rows, last = "", None
        for q in qs:
            if q["section"] != last:
                rows += f'<div class="qsec">{E(q["section"])}</div>'
                last = q["section"]
            first = q["text"][:110]
            rows += (f'<details class="q"><summary><div class="qhead">'
                     f'<span class="lab">{q["num"]}</span><h3>{E(first)}</h3></div>'
                     f'</summary>'
                     f'<form method="post" action="/intake-answer">'
                     f'{hidden("file", f.name)}{hidden("num", q["num"])}'
                     f'{hidden("tab", "questions")}'
                     f'<textarea name="answer" rows="3" '
                     f'placeholder="Answer. Dictation welcome."></textarea>'
                     f'<div class="form-act"><button class="btn pri">answer</button></div>'
                     f'</form></details>')
        out += panel(f.stem.replace("-", " ").lower(), rows, len(qs))
    if not out and not c["queue"]:
        return panel("questions", '<p class="empty">Nothing open.</p>')
    return out


def m_capture(c):
    return panel("write something down",
                 '<form method="post" action="/capture">'
                 + hidden("tab", "inbox")
                 + '<textarea id="capture" name="note" rows="8" '
                   'placeholder="A note, a correction, '
                   'a lead, anything you do not want to lose."></textarea>'
                   '<div class="form-act"><button class="btn pri">send</button></div>'
                   '</form>')


def m_waiting(c):
    rows = "".join(
        f'<div class="row"><span class="dot" style="margin-top:7px"></span>'
        f'<div class="body"><div class="line"><span class="t">{E(x)}</span></div></div></div>'
        for x in c["inbox"])
    body = (f'<div class="rows">{rows}</div>' if rows
            else '<p class="empty">Nothing waiting.</p>')
    return panel("not picked up yet", body, len(c["inbox"]))


# ----------------------------------------------------------- handbook module

def tree_html(paths, current):
    root = {}
    for rel in paths:
        node = root
        parts = rel.split(os.sep)
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("", []).append((parts[-1], rel))

    def render(node, depth):
        out = ""
        for name, rel in node.get("", []):
            on = " class=\"on\"" if rel == current else ""
            out += (f'<a href="/handbook?page={urllib.parse.quote(rel)}"{on} '
                    f'title="{E(rel)}">{E(name[:-3])}</a>')
        for key in sorted((k for k in node if k != ""), key=lambda k: (k.startswith("@"), k)):
            open_ = " open" if current and current.startswith(key + os.sep) else ""
            out += (f'<details{open_}><summary>{E(key.lstrip("@"))}</summary>'
                    f'<div>{render(node[key], depth + 1)}</div></details>')
        return out
    return f'<div class="tree">{render(root, 0)}</div>'


def diff_html(text):
    out, lines = [], text.splitlines()
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES]
        trunc = f'<div class="hunk">diff truncated at {DIFF_MAX_LINES} lines</div>'
    else:
        trunc = ""
    head, i = [], 0
    while i < len(lines) and not lines[i].startswith("diff --git"):
        head.append(lines[i])
        i += 1
    if head:
        out.append(f'<div class="stat-lines">{E(chr(10).join(head).strip())}</div>')
    for line in lines[i:]:
        if line.startswith("diff --git"):
            out.append(f'<div class="fh">{E(line[11:])}</div>')
        elif line.startswith("@@"):
            out.append(f'<div class="hunk">{E(line)}</div>')
        elif line.startswith(("index ", "--- ", "+++ ", "new file mode",
                              "deleted file mode", "similarity index",
                              "rename from", "rename to")):
            continue
        elif line.startswith("+"):
            out.append(f'<div class="l a">{E(line)}</div>')
        elif line.startswith("-"):
            out.append(f'<div class="l d">{E(line)}</div>')
        else:
            out.append(f'<div class="l">{E(line)}</div>')
    return f'<div class="diff">{"".join(out)}{trunc}</div>'


def day_label(iso):
    try:
        d = datetime.date.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime("%a %-d %b").lower()


def comment_well(field, value, placeholder):
    return (f'<div class="cwell"><form method="post" action="/comment">'
            f'{hidden(field, value)}{hidden("tab", "handbook")}'
            f'<textarea name="comment" rows="3" placeholder="{E(placeholder)}"></textarea>'
            f'<div class="form-act"><button class="btn pri">comment</button></div>'
            f'</form></div>')


def hb_keys(view):
    def key(name, href):
        return (f'<a class="key{" on" if view == name else ""}" '
                f'href="{href}">{name}</a>')
    return ('<div class="keys row sm">'
            + key("pages", "/handbook?tree=1")
            + key("changes", "/handbook?changes=1")
            + key("graph", "/handbook?graph=1") + "</div>")


def m_handbook(c):
    hb = c["hb"]
    if hb["graph"]:
        view = "graph"
    elif hb["changes"] or hb["commit"]:
        view = "changes"
    else:
        view = "pages"
    keys = hb_keys(view)

    def head(extra="", count=""):
        return (f'<section class="panel"><div class="phead"><h2>handbook</h2>{keys}'
                f'<div class="hctl">{extra}'
                f'<span class="count">{E(count)}</span></div></div></section>')

    if view == "graph":
        legend = "".join(
            f'<span><i style="background:{tone}"></i>{E(name)}</span>'
            for name, tone in DOMAIN_TONES.items())
        return (head("", f'{len(c["pages"])} pages')
                + '<section class="panel flush"><div class="graph">'
                '<canvas id="vgraph"></canvas><div class="gtip" id="gtip"></div>'
                f'</div><div class="glegend">{legend}</div></section>')

    if view == "changes":
        log = c["log"]
        repo = hb["repo"]
        top = head("", hb["commit"] or "recent") + repo_keys(c["repos"], repo)
        if isinstance(log, dict):
            return top + panel("", f'<p class="empty">{E(log["error"])}</p>')
        merged = repo not in c["repos"]
        rows, day = "", None
        for r in log:
            if r["date"] != day:
                day = r["date"]
                rows += f'<div class="cday">{E(day_label(day))}</div>'
            chip = (f'<span class="chip repo">{E(r["repo"])}</span>' if merged else "")
            on = " on" if r["hash"] == hb["commit"] and r["repo"] == repo else ""
            rows += (f'<a class="commit{on}" href="/handbook?commit={E(r["hash"])}'
                     f'&amp;repo={urllib.parse.quote(r["repo"])}">'
                     f'<span class="h">{E(r["hash"])}</span>{chip}'
                     f'<span class="chip">{E(r["kind"])}</span>'
                     f'<span class="s">{E(r["subject"])}</span></a>')
        body = f'<div class="commits">{rows}</div>' if rows else (
            '<p class="empty" style="padding:16px">No commits.</p>')
        if hb["commit"]:
            shown = git_show(c["repos"][hb["show_repo"]], hb["commit"])
            body += (f'<p class="empty" style="padding:16px">{E(shown["error"])}</p>'
                     if isinstance(shown, dict) else diff_html(shown))
            body += ('<div style="padding:0 16px 18px">'
                     + comment_well("commit", hb["commit"],
                                    "A comment on this commit. It lands in the "
                                    "inbox tagged with the hash.") + "</div>")
        return top + f'<section class="panel flush">{body}</section>'

    rel = hb["page"]
    path = safe_page(rel)
    tree = tree_html(c["pages"], rel if path else "")
    if path:
        doc = (f'<div class="doc">{md_page(path.read_text(), page_index())}'
               + comment_well("page", rel,
                              "A comment on this page. It lands in the inbox "
                              "tagged with the path.") + "</div>")
    else:
        doc = '<div class="doc"><p class="empty">Pick a page.</p></div>'
    return (head("", rel if path else "the tree")
            + f'<section class="panel flush"><div class="hb">{HB_TOGGLE}'
            f'<button class="treetog" type="button" aria-expanded="false" '
            f"onclick=\"this.closest('.hb').classList.toggle('open');"
            f"this.setAttribute('aria-expanded',"
            f"this.closest('.hb').classList.contains('open'))\">"
            f'browse pages</button>'
            f'{tree}{doc}</div></section>')


HB_TOGGLE = ('<button class="hbtog" id="hbtog" type="button" onclick="hbtog(this)" '
             'aria-expanded="true" aria-label="hide the file tree" '
             'title="collapse the tree, widen the page">'
             '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" '
             'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
             'stroke-linejoin="round"><path d="M10 3 5 8l5 5"/></svg></button>')


def repo_keys(repos, active):
    def key(name, label):
        on = " on" if name == active else ""
        return (f'<a class="key{on}" href="/handbook?changes=1'
                f'&amp;repo={urllib.parse.quote(name)}">{E(label)}</a>')
    return ('<section class="panel repos"><div class="keys row sm">'
            + key("all", "all") + "".join(key(k, k) for k in repos)
            + "</div></section>")


SECTIONS = [
    ("today", "today", None, [m_anchor, m_stats, m_clocks, m_focus]),
    ("tasks", "tasks", work_count, [m_board]),
    ("handbook", "handbook", lambda c: len(c["pages"]), [m_handbook]),
    ("pipeline", "pipeline", lambda c: len(c["deals"]), [m_pipeline]),
    ("questions", "questions", lambda c: c["q_total"], [m_queue, m_intake]),
    ("inbox", "inbox", lambda c: len(c["inbox"]), [m_capture, m_waiting]),
]


def context(hb=None):
    sections = todo_sections()
    board = [t for s in sections for t in s["items"]
             if not s["title"].startswith("Clocks") and s["title"] != "Done"]
    items = [t for t in board if not t["done"]]
    deals_raw = cached("deals", 60, attio_deals) if on("attio") else []
    deals = deals_raw if isinstance(deals_raw, list) else []
    queue = open_queue_questions()
    q_total = len(queue) + sum(
        1 for f in intake_files() for q in intake_questions(f) if not q["answered"])
    clocks = next((s["clocks"] for s in sections if s["title"].startswith("Clocks")), [])
    hb = hb or {"page": "", "changes": False, "commit": "", "graph": False,
                "repo": "all", "show_repo": CONFIG["wordmark"]}
    repos = git_repos()
    if hb["show_repo"] not in repos:
        hb["show_repo"] = next(iter(repos), CONFIG["wordmark"])
    return {
        "sections": sections, "items": items, "all_items": board,
        "clocks": clock_rows(clocks, sections),
        "focus": focus_picks(),
        "deals_raw": deals_raw, "deals": deals,
        "won": sum(1 for d in deals if d["stage"].startswith("Won")),
        "closed": sum(1 for d in deals if d["stage"].startswith(("Won", "Lost"))),
        "steps": cached("steps", 60, attio_next_steps) if deals else {},
        "build": cached("build", 120, gh_build) if on("github") else {"review": [], "mine": []},
        "queue": queue, "q_total": q_total,
        "inbox": inbox_entries(),
        "drift": cached("drift", 300, drift),
        "hb": hb, "pages": cached("pages", 30, vault_pages), "repos": repos,
        "log": (repo_timeline(repos, hb["repo"])
                if (hb["changes"] or hb["commit"]) else []),
    }


def runs_strip():
    if CONFIG.get("runs") == {}:
        return ""
    out = ""
    for label, path, hours, window, nxt in (
            ("brief", BRIEF, BRIEF_STALE_HOURS, None, next_brief),
            ("sync", CAPTURE_LOG, SYNC_STALE_HOURS, SYNC_WINDOW, next_sync)):
        last, late, ts = run_stat(path, hours, window)
        logged = run_log_last(CONFIG["runs"].get(label))
        if logged and (ts is None or logged[0] >= ts - 3600):
            ts = logged[0]
            last = time.strftime("%H:%M", time.localtime(ts))
            late = (time.time() - ts) / 3600 > hours and not (
                window and not (window[0] <= time.localtime().tm_hour < window[1]))
        when, when_label = nxt()
        if logged and logged[1].lower().startswith("fail"):
            out += (f'<div class="run warn"><div class="lab">{label}</div>'
                    f'<div class="v">failed</div>'
                    f'<div class="c" title="{E(logged[1])}">{E(logged[1][:40])}</div></div>')
        elif late:
            out += (f'<div class="run warn"><div class="lab">{label}</div>'
                    f'<div class="v">{late_label(ts)}</div>'
                    f'<div class="c">last run {last}</div></div>')
        else:
            out += (f'<div class="run"><div class="lab">{label}</div>'
                    f'<div class="v" data-until="{int(when.timestamp())}">...</div>'
                    f'<div class="c">next {label} {when_label}</div></div>')
    return f'<div class="runs">{out}</div>'


def active_sections():
    on = CONFIG.get("sections") or []
    return [x for x in SECTIONS if not on or x[0] in on]


def logo_img():
    if not CONFIG["logo"]:
        return ""
    return (f'<img class="logo" src="/vault/{urllib.parse.quote(CONFIG["logo"])}" alt="" '
            f'onerror="this.remove()">')


def rail_keys(c):
    keys = ""
    for i, (key, label, count, _) in enumerate(active_sections()):
        n = count(c) if count else None
        badge = f'<span class="n">{n}</span>' if n is not None else ""
        click = "hbkey()" if key == "handbook" else f"tab('{key}')"
        keys += (f'<button class="key" role="tab" aria-selected="false" data-t="{key}" onclick="{click}">'
                 f'<span class="d">{i + 1}</span>{E(label)}{badge}</button>')
    return (f'<div class="keys">{keys}</div>'
            f'<a class="stamp setup-link" href="/setup" title="name, sections, repos">settings</a>'
            f'<a class="stamp setup-link" href="/refresh" onclick="this.href=\'/refresh?tab=\'+(location.hash||\'#today\').slice(1)" '
            f'title="fetch Linear, GitHub and Attio again now">refresh</a>')


def fragment(name, hb=None):
    """One tab's inner HTML, or the rail's live parts. No shell, no rail chrome."""
    c = context(hb)
    if name == "rail":
        return runs_strip() + rail_keys(c)
    for key, _, _, modules in active_sections():
        if key == name:
            return "".join(m(c) for m in modules)
    return ""


SKELETON = {
    "90-Meta/Questions.md": "# Questions\n\nThe standing inbox between you and your agent. "
    "It files what only you can answer; you answer under `**A:**`.\n\n## Open\n\n"
    "## Answered\n\n## Inbox from dashboard\n",
    "90-Meta/Assistant-Memory.md": "# Assistant memory\n\nWhat the morning run carries "
    "between days.\n\n## Tier-1 carry\n\n### Bench\n",
    "90-Meta/Capture-Log.md": "# Capture log\n\nOne line per capture run.\n",
}


def bootstrap_prompt(projects):
    names = ", ".join(projects) or "the projects I name"
    return (f"You are my handbook agent. The vault is the folder {VAULT}. "
            "It is plain markdown, and a dashboard renders it, so keep the files in the shapes "
            "they already have.\n\n"
            f"Todo.md is the board: one `## Project` section per project ({names}), "
            "a `## Clocks` section for dated commitments as `- Project — what is due YYYY-MM-DD "
            "· source`, and `## Done` at the end. A task line is "
            "`- [ ] **Short title** - one or two sentences · added MM-DD`. Titles are 60 "
            "characters or fewer and never repeat the project name.\n\n"
            "90-Meta/Questions.md is our inbox. When you need something only I know, add "
            "`## Q-NNN · title · asked YYYY-MM-DD` under `## Open` with the question and an empty "
            "`**A:**` line, and read my answers there at the start of every session. Notes I "
            "leave under `## Inbox from dashboard` are yours to file, then delete.\n\n"
            "90-Meta/Assistant-Memory.md is what you carry between days: under `## Tier-1 carry` "
            "the two to five tasks that matter today, one per line as `- task — Nd`, and under "
            "`### Bench` the next three, numbered.\n\n"
            "Start now: ask me five questions about each project, file them in Questions.md, "
            "write one page per project at the vault root with what you already know, and put "
            "three honest first tasks on the board.")


def setup_page(saved=False):
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    on = set(CONFIG.get("sections") or [x[0] for x in SECTIONS])
    boxes = "".join(
        f'<label class="chk"><input type="checkbox" name="sections" value="{key}"'
        f'{" checked" if key in on else ""}> {E(label)}</label>'
        for key, label, _, _ in SECTIONS)
    projects = [x["title"] for x in todo_sections()
                if not x["title"].startswith("Clocks") and x["title"] != "Done"]
    fresh = not TODO.exists()
    intro = ("<p>Welcome. Three steps: say who you are, pick what the dashboard shows, "
             "then hand the prompt below to your agent. It writes the files; this page reads them."
             "</p>" if fresh else "<p>Name, sections, repos. Paths and secrets stay in "
             f"<code>{E(str(CONFIG_FILE))}</code>.</p>")
    note = '<p class="ok">Saved.</p>' if saved else ""
    css = CSS.replace("__ACCENT__", CONFIG["accent"]).replace(
        "__ACCENT_DARK__", CONFIG["accent_dark"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(CONFIG["wordmark"])} setup</title><style>{css}
.setup{{max-width:680px}}.setup label{{display:block;margin:14px 0 4px;color:var(--ink-2);font-size:12px}}
.setup input[type=text],.setup textarea{{width:100%}}.setup .chk{{display:inline-block;margin:6px 16px 0 0;color:var(--ink)}}
.setup pre{{white-space:pre-wrap;font:12.5px/1.5 var(--mono);background:var(--panel-2);padding:12px;border:1px solid var(--line-soft)}}
.setup .ok{{color:var(--ok)}}.wrap{{max-width:720px;margin:32px auto;padding:0 16px}}</style></head><body><div class="wrap">
<section class="panel setup"><div class="phead"><h2>{"welcome" if fresh else "settings"}</h2>
<div class="hctl"><a class="out" href="/">back</a></div></div>{intro}{note}
<form method="post" action="/setup">
<label for="owner">your name, for the greeting</label>
<input type="text" id="owner" name="owner" value="{E(cfg.get("owner", ""))}">
<label>sections to show</label>{boxes}
<label for="projects">projects, one per line{"" if fresh else " (adds a section for any new one)"}</label>
<textarea id="projects" name="projects" rows="4">{E(chr(10).join(projects))}</textarea>
<label for="repos">GitHub repos to watch, one per line as owner/name</label>
<textarea id="repos" name="repos" rows="3">{E(chr(10).join(cfg.get("repos", [])))}</textarea>
<div class="form-act"><button class="btn pri">save</button></div></form>
<label>the prompt for your agent</label>
<pre id="bp">{E(bootstrap_prompt(projects))}</pre>
<div class="form-act"><button type="button" class="btn" onclick="navigator.clipboard.writeText(document.getElementById('bp').textContent)">copy</button></div>
</section></div></body></html>"""


def save_setup(form):
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    cfg["owner"] = form.get("owner", [""])[0].strip()
    cfg["sections"] = [x for x in form.get("sections", []) if x in {k for k, *_ in SECTIONS}]
    cfg["repos"] = [r.strip() for r in form.get("repos", [""])[0].splitlines()
                    if re.fullmatch(r"[\w.-]+/[\w.-]+", r.strip())]
    save(CONFIG_FILE, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    projects = [x.strip() for x in form.get("projects", [""])[0].splitlines()
                if x.strip() and not x.strip().startswith("#")]
    if not TODO.exists():
        TODO.parent.mkdir(parents=True, exist_ok=True)
        body = "# Todo\n\n## Clocks\n\n" + "".join(f"## {p}\n\n" for p in projects) + "## Done\n"
        save(TODO, body)
    else:
        have = {x["title"] for x in todo_sections()}
        new = [p for p in projects if p not in have]
        if new:
            text = TODO.read_text()
            at = text.find("\n## Done")
            add = "".join(f"\n## {p}\n" for p in new)
            save(TODO, text[:at] + add + text[at:] if at != -1 else text + add)
    for rel, body in SKELETON.items():
        target = VAULT / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
    home = VAULT / CONFIG["home_page"]
    if not home.exists():
        home.write_text("# Home\n\n" + "".join(f"- [[{p}]]\n" for p in projects))
    load_config()
    REPOS[:] = CONFIG["repos"]
    _cache.clear()


def page(active="today", hb=None):
    c = context(hb)
    panes, names = "", []
    for key, _, _, modules in active_sections():
        panes += (f'<div class="tab" id="t-{key}">'
                  + "".join(m(c) for m in modules) + "</div>")
        names.append(key)
    keys = rail_keys(c)
    css = CSS.replace("__ACCENT__", CONFIG["accent"]).replace(
        "__ACCENT_DARK__", CONFIG["accent_dark"])
    js = (f'const TABNAMES={json.dumps(names)};'
          f'const TABKEYS={json.dumps([str(i + 1) for i in range(len(names))])};'
          f'const ACTIVE={json.dumps(active)};'
          f'const GTONE={json.dumps(DOMAIN_TONES)};' + JS)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{E(CONFIG["wordmark"])}</title>
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="{E(CONFIG["wordmark"])}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
<style>{css}</style><script>{js}</script></head><body>
{ICON_DEFS}{GIT_MARK_DEFS}
<div class="shell">
<nav class="rail">
  <span class="wordmark" onclick="pulse()" title="{E(CONFIG["wordmark"])}">{logo_img()}{E(CONFIG["wordmark"])}</span>
  <span class="stamp">{time.strftime("%a %-d %b %Y").lower()}</span>
  {runs_strip()}
  {keys}
</nav>
<main class="main">{panes}</main>
</div></body></html>"""


# ------------------------------------------------------------------- serving

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _html(self, body, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            if not TODO.exists():
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.end_headers()
                return
            self._html(page())
        elif url.path == "/setup":
            self._html(setup_page("saved" in q))
        elif url.path == "/refresh":
            _cache.clear()
            self.send_response(303)
            self.send_header("Location", "/#" + (q.get("tab", ["today"])[0]))
            self.end_headers()
        elif url.path == "/brief" and BRIEF.exists():
            self._html(BRIEF.read_text())
        elif url.path == "/handbook-graph":
            self._json(cached("graph", 120, vault_graph))
        elif url.path == "/events":
            self._events()
        elif url.path == "/fragment":
            self._html(fragment(q.get("tab", [""])[0]))
        elif url.path == "/handbook":
            commit = q.get("commit", [""])[0]
            if commit and not re.fullmatch(r"[0-9a-f]{7,40}", commit):
                commit = ""
            repos = git_repos()
            repo = q.get("repo", [""])[0]
            repo = repo if repo in repos else "all"
            rel = q.get("page", [""])[0]
            if not rel and not commit and not (q.keys() & {"changes", "graph", "tree"}):
                rel = CONFIG["home_page"]
            hb = {"page": rel, "changes": "changes" in q, "graph": "graph" in q,
                  "commit": commit, "repo": repo,
                  "show_repo": repo if repo in repos else CONFIG["wordmark"]}
            self._html(fragment("handbook", hb) if "partial" in q
                       else page("handbook", hb))
        elif url.path.startswith("/assets/"):
            self._asset(urllib.parse.unquote(url.path[len("/assets/"):]))
        elif url.path.startswith("/vault/"):
            self._vault_asset(urllib.parse.unquote(url.path[len("/vault/"):]))
        else:
            self._html("not found", 404)

    def _events(self):
        stream = queue.Queue(maxsize=64)
        sse_add(stream)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": open\n\n")
            self.wfile.flush()
            while True:
                try:
                    source = stream.get(timeout=SSE_PING)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if source is None:
                    break
                self.wfile.write(f"data: {source}\n\n".encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            sse_drop(stream)

    def _vault_asset(self, rel):
        """An image inside the vault (the logo). Refuses anything outside it."""
        try:
            p = (VAULT / rel).resolve()
        except OSError:
            p = None
        root = str(VAULT.resolve())
        ok = p and str(p).startswith(root + os.sep) and p.is_file() and \
            p.suffix.lower() in (".svg", ".png", ".jpg", ".jpeg", ".webp")
        if not ok:
            self._html("not found", 404)
            return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ASSET_TYPES.get(p.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _asset(self, name):
        p = asset_path(name)
        ctype = ASSET_TYPES.get(p.suffix.lower()) if p else None
        if not p or not ctype:
            self._html("not found", 404)
            return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self._html("forbidden", 403)
            return
        with _write_lock:
            self._post()

    def _post(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        get = lambda k: form.get(k, [""])[0]
        tab = get("tab") or "today"
        new_id = None
        if self.path == "/task":
            if re.fullmatch(r"[0-9a-f]{12}", get("id")):
                n = int(get("n")) if get("n").isdigit() else 0
                new_id = close_task(get("id"), get("action"), n)
                if new_id and get("action") == "unreframe":
                    drop_inbox_line('reframe task "')
        elif self.path == "/reframe":
            if re.fullmatch(r"[0-9a-f]{12}", get("id")) and get("comment").strip():
                append_note(f'reframe task "{get("title")}": {get("comment")}')
                new_id = close_task(get("id"), "reframe")
        elif self.path == "/answer":
            if re.fullmatch(r"Q-\d+", get("qid")) and get("answer").strip():
                write_queue_answer(get("qid"), get("answer"))
        elif self.path == "/intake-answer":
            fname = get("file")
            target = META / fname
            if (re.fullmatch(r"Founder-Intake-[\w-]+\.md", fname) and target.exists()
                    and get("num").isdigit() and get("answer").strip()):
                write_intake_answer(target, int(get("num")), get("answer"))
        elif self.path == "/clock":
            if re.fullmatch(r"[0-9a-f]{12}", get("id")):
                resolve_clock(get("id"))
        elif self.path == "/capture":
            if get("note").strip():
                append_note(get("note"))
        elif self.path == "/setup":
            save_setup(form)
            self.send_response(303)
            self.send_header("Location", "/setup?saved" if TODO.exists() else "/")
            self.end_headers()
            return
        elif self.path == "/archive":
            archive_done()
        elif self.path == "/add-task":
            add_task(get("section"), get("title"), get("note"))
        elif self.path == "/edit-task":
            if re.fullmatch(r"[0-9a-f]{12}", get("id")):
                new_id = edit_task(get("id"), get("title"), get("note"))
        elif self.path == "/comment":
            rel, commit, note = get("page"), get("commit"), get("comment").strip()
            if note and re.fullmatch(r"[0-9a-f]{7,40}", commit):
                append_inbox(f"[commit: {commit}] {note}")
            elif note and safe_page(rel):
                append_inbox(f"[page: {rel}] {note}")
        if self.headers.get("X-Row"):
            t = find_task(new_id) if new_id else None
            body = task_row(t, tab) if t else ""
            if body and tab == "today" and get("action") in ("done", "notneeded"):
                body += promoted_row()
            self._html(body)
            return
        self.send_response(303)
        self.send_header("Location", f"/#{tab}")
        self.end_headers()


def selftest():
    t, r, s = split_task("**Ship the short links** — verify on prod · added 08-19, verified 08-20")
    assert t == "Ship the short links", t
    assert r == "verify on prod", r
    assert s == "added 08-19, verified 08-20", s
    t, r, s = split_task("Acme/Northwind: ask Sam for the gateway vars · from Sync, 08-11")
    assert t == "Acme/Northwind", t
    assert s == "from Sync, 08-11", s
    long_title = "**" + "word " * 40 + "** tail"
    t, r, _ = split_task(long_title)
    assert len(t) <= 100 and r.endswith("tail"), (len(t), r)
    assert split_task("plain line with no source")[2] == ""
    assert age_days("added 08-19, verified 08-20") is not None
    assert age_days("no date here") is None
    items = [{"id": "a", "title": "Acme/Northwind: give the gateway the payment return URL",
              "rest": "needed before any end to end payment test"},
             {"id": "b", "title": "Acme/Northwind: hosting decided", "rest": "railway"}]
    assert match_task("Acme/Northwind: give the gateway the payment return URL", items)["id"] == "a"
    assert match_task("something unrelated entirely", items) is None
    assert task_id("- [ ] x") == task_id("- [ ] x\n")

    multi = "**A thing** — body · added 08-20 · flagged stale 08-27 — releases since"
    t, _, s = split_task(multi)
    assert t == "A thing", t
    assert s == "added 08-20 · flagged stale 08-27 — releases since", s
    st = task_state(multi)
    assert len(st["flags"]) == 1 and st["flags"][0]["n"] == 1, st
    assert not st["flags"][0]["unsure"]
    assert drop_chunk(multi, 1) == "**A thing** — body · added 08-20", drop_chunk(multi, 1)
    closed = "**A thing** — body · added 08-20 · closed from dashboard 08-28: not needed"
    st = task_state(closed)
    assert st["closed_here"] and st["not_needed"], st
    assert drop_chunk(closed, 1) == "**A thing** — body · added 08-20"
    unsure = "**A thing** · flagged unsure via dashboard 08-28"
    assert task_state(unsure)["flags"][0]["unsure"]
    assert task_state("**A thing** · reopened from dashboard 08-28")["reopened"]
    assert task_state("**A thing** · reframe asked via dashboard 08-28")["reframed"]

    md = md_block("**Probably done:**\n1. first → \"a\"\n2. second\n\n"
                  "8. eight\n\n- bullet [[Note|alias]]\n\nplain <b>text</b>")
    assert "<strong>Probably done:</strong>" in md, md
    assert "&quot;a&quot;" in md, md
    assert md.count("<ol") == 2 and '<ol start="8">' in md, md
    assert "<ul><li>bullet alias</li></ul>" in md, md
    assert "&lt;b&gt;text&lt;/b&gt;" in md, md
    assert run_stat(pathlib.Path("/nonexistent"), 1)[:2] == ("--:--", True)

    idx = {"handoff": "90-Meta/Handoff.md"}
    doc = md_page("---\ncanon: working\nstatus: active\n---\n"
                  "# Head\n\n> [!todo] missing thing\n> the source\n\n"
                  "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
                  "- see [[Handoff]] and [[Nope]]\n\n`code` and **bold**\n", idx)
    assert 'canon working · status active' in doc, doc
    assert "<h1>Head</h1>" in doc, doc
    assert 'class="well todo"' in doc and "missing thing" in doc, doc
    assert "<table>" in doc and "<th>a</th>" in doc, doc
    assert '/handbook?page=90-Meta/Handoff.md' in doc, doc
    assert ">Nope<" not in doc and "Nope" in doc, doc
    assert "<code>code</code>" in doc and "<strong>bold</strong>" in doc, doc
    assert safe_page("../../etc/passwd") is None
    assert safe_page("/etc/passwd") is None
    assert safe_page("Todo.md") is not None
    assert safe_page("Todo.txt") is None

    d = diff_html("commit x\n file | 2 +-\ndiff --git a/x.md b/x.md\n"
                  "index 1..2\n@@ -1 +1 @@\n-old\n+new\n")
    assert 'class="l a">+new' in d, d
    assert 'class="l d">-old' in d, d
    assert 'class="hunk">@@' in d and "index 1..2" not in d, d

    assert extra("anchor") == "" or VAULT.joinpath(".dashboard", "extras.py").exists()
    assert "Good " in greeting()
    assert day_title(2) == "second day as a tier-1 pick", day_title(2)
    assert day_title(9) == "9th day as a tier-1 pick", day_title(9)
    assert "g-open" in git_mark("open") and "g-draft" in git_mark("draft")
    assert ">live<" in git_mark("deployed") and git_mark("nope") == ""
    assert gh_row({"state": "OPEN", "isDraft": True})["state"] == "draft"
    assert gh_row({"state": "OPEN"})["state"] == "open"
    assert gh_row({}, "merged")["state"] == "merged"
    assert source_icon({"project": "ACME", "source": "from Sync, 08-11"}) == "granola"
    CONFIG["icon_projects"] = {"Dayjob": "acme"}
    assert source_icon({"project": "Dayjob", "source": "added 08-11"}) == "acme"
    CONFIG["icon_projects"] = {}
    assert source_icon({"project": "ACME", "source": "added 08-11"}) == "claude"
    assert bench_prefix("Beta: record the videos", None) == "Beta"
    assert bench_prefix("a line with no project", None) == ""
    assert bench_prefix("x", {"project": "ACME — client build, week 5 of 8"}) == "ACME"
    assert short_project("Acme/Northwind: the portal") == "Acme/Northwind"
    assert day_label("2026-08-28") == "fri 28 aug", day_label("2026-08-28")
    assert day_label("nope") == "nope"

    g = vault_graph()
    assert len(g["nodes"]) > 5, len(g["nodes"])
    assert len(g["edges"]) > 3, len(g["edges"])
    assert all(set(n) == {"id", "path", "domain", "links_count"} for n in g["nodes"])
    assert all(0 <= i < len(g["nodes"]) and 0 <= j < len(g["nodes"])
               for i, j in g["edges"])
    linked = {i for e in g["edges"] for i in e}
    assert all((n["links_count"] > 0) == (k in linked)
               for k, n in enumerate(g["nodes"]))

    hits = []

    def slow():
        hits.append(1)
        return len(hits) - 1
    assert cached("swr", 0.05, slow) == 0
    time.sleep(0.06)
    assert cached("swr", 0.05, slow) == 0, "stale value must be served at once"
    for _ in range(100):
        if _cache["swr"][1] == 1:
            break
        time.sleep(0.02)
    assert cached("swr", 999, slow) == 1, "background refresh must land"

    assert iso_days(datetime.datetime.utcnow().isoformat() + "Z") == 0
    assert iso_days("nope") is None and iso_days(None) is None
    assert money({"currency_value": 670000, "currency_code": "MXN"}) == "670,000 MXN"
    assert money({}) == "" and money({"currency_value": None}) == ""
    assert stage_rank("Lead") < stage_rank("In Progress") < stage_rank("Won")
    assert stage_rank("Won") < stage_rank("Zebra"), "unknown stages sort last"
    card = deal_card({"name": "A deal", "value": "60,000 MXN", "days": 3,
                      "url": CONFIG["attio_url"] + "/x/deals/record/abc"}, "call them")
    assert 'class="card"' in card and 'href="https://app.attio.com/x/' in card, card
    assert ">60,000 MXN<" in card and ">3d<" in card, card
    assert "Next: call them" in card, card
    assert "<a" not in deal_card({"name": "n", "value": "", "days": None,
                                  "url": ""}, None), "no id means no link"

    repos = git_repos()
    if repos:
        assert CONFIG["wordmark"] in repos, repos
        assert all((p / ".git").exists() for p in repos.values())
        assert "mensajero" not in repos or (VAULT.parent / "mensajero" / ".git").exists()
        for bad in ("all", "", "../board-ia", "handbook; rm", "nope"):
            assert bad not in repos or bad == "all", bad
        rows = repo_timeline(repos, CONFIG["wordmark"])
        assert rows and all(r["repo"] == CONFIG["wordmark"] for r in rows), rows[:1]
        assert all(re.fullmatch(r"[0-9a-f]{7,40}", r["hash"]) for r in rows)
        merged = repo_timeline(repos, "all")
        assert len(merged) <= LOG_MERGED_MAX, len(merged)
        assert merged == sorted(merged, key=lambda r: r["at"], reverse=True)
        if len(repos) > 1:
            assert len({r["repo"] for r in merged}) > 1, "merged must span repos"

    assert ASSET_TYPES.get(".py") is None, "only image types are servable"
    tile = icon("granola", "from Sync")
    if "granola" in ASSET_ICONS:
        assert 'class="ic tile" src="/assets/granola.png"' in tile, tile
    assert "i-mail" in icon("mail", "mail"), "drawn tiles stay for mail"
    assert icon("nope") == ""

    row = task_row({"id": "a" * 12, "title": "T", "rest": "", "source": "added 08-20",
                    "age": 8, "done": False, "project": "P", "flags": [],
                    "closed_here": False, "not_needed": False, "reframed": False,
                    "reopened": False}, "today")
    assert 'class="meta-r"' in row and ">8d<" in row, row
    assert 'class="mk"' in row and ">more<" in row, row
    assert 'aria-expanded="false"' in row, row
    assert 'class="tick"' in row, row
    assert bench_row("a line with nothing matching it at all here",
                     {"all_items": []}).count('class="meta-r"') == 1

    assert "hash" in g and len(g["hash"]) == 16, g.get("hash")

    test_intake_status()
    test_next_up()
    test_bench_promotion()
    test_group_toggle()
    test_watch_stamps()
    test_fragments()
    test_linear_panel()
    test_sse_cap()
    test_config_overlay()
    test_starter_vault()
    test_archive_and_setup()
    test_run_log()
    test_linear_order()
    test_handbook_roots()
    test_add_edit()
    print("ok")


def test_add_edit():
    global TODO
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    keep = TODO
    TODO = d / "Todo.md"
    try:
        TODO.write_text("# Todo\n\n## Clocks\n\n## Site\n\n- [ ] **Old one** - stays · added 09-01\n\n## Done\n")
        tid = add_task("Site", "Ship the thing", "before Friday")
        text = TODO.read_text()
        assert "- [ ] **Ship the thing** - before Friday · added " in text and tid, text
        assert text.index("Ship the thing") > text.index("Old one") and text.index("Ship the thing") < text.index("## Done")
        assert add_task("New group", "First line") and "## New group\n" in TODO.read_text()
        assert TODO.read_text().index("## New group") < TODO.read_text().index("## Done")
        assert add_task("Done", "nope") is None and add_task("Site", "") is None
        new = edit_task(tid, "Ship the thing, tested", "on staging")
        assert new and "**Ship the thing, tested** - on staging · added " in TODO.read_text()
        assert "before Friday" not in TODO.read_text()
    finally:
        TODO = keep
        shutil.rmtree(d)


def test_handbook_roots():
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "sub").mkdir()
    (d / "sub" / "Page.md").write_text("# p")
    keep = CONFIG["handbooks"]
    CONFIG["handbooks"] = [{"name": "X", "path": str(d)}, {"name": "bad/name", "path": str(d)}]
    try:
        assert list(hb_roots()) == ["X"]
        assert "@X/sub/Page.md" in vault_pages()
        assert safe_page("@X/sub/Page.md") == (d / "sub" / "Page.md").resolve()
        assert safe_page("@X/../etc/passwd.md") is None and safe_page("@Nope/a.md") is None
        assert "<summary>X</summary>" in tree_html(["@X/sub/Page.md"], "")
    finally:
        CONFIG["handbooks"] = keep
        shutil.rmtree(d)


def test_linear_order():
    got = [{"identifier": "A-1", "priority": 0, "sortOrder": 1, "state": {"type": "unstarted"}},
           {"identifier": "A-2", "priority": 2, "sortOrder": 5, "state": {"type": "unstarted"}},
           {"identifier": "A-3", "priority": 2, "sortOrder": 2, "state": {"type": "unstarted"}},
           {"identifier": "A-4", "priority": 4, "sortOrder": 0, "state": {"type": "started", "name": "In Progress"}},
           {"identifier": "A-5", "priority": 1, "sortOrder": 0, "state": {"type": "started", "name": "In Review"}},
           {"identifier": "A-6", "priority": 1, "sortOrder": 0, "state": {"type": "triage", "name": "Triage"}}]
    assert [x["identifier"] for x in linear_ordered(got)] == ["A-4", "A-3", "A-2", "A-1", "A-5"]
    assert [x["identifier"] for x in linear_triage(got)] == ["A-6"]
    assert ci_state({"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}) == "green"
    assert ci_state({"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}) == "red"
    assert ci_state({"statusCheckRollup": [{"status": "IN_PROGRESS"}]}) == "pending" and ci_state({}) == ""


def test_run_log():
    global META
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    keep = META
    META = d
    try:
        (d / "Runs.md").write_text("# Runs\n\n- 2026-09-04 07:15 · morning-brief · ok · 3 files\n"
                                   "- 2026-09-04 12:02 · capture-sync · failed: Reminders timed out\n")
        assert run_log_last("capture-sync")[1] == "failed: Reminders timed out"
        assert run_log_last("morning-brief")[1] == "ok"
        assert run_log_last("nope") is None
        assert RUN_LINE.match("- 2026-09-04 07:15 · morning-brief · ok").group(3) == "ok"
    finally:
        META = keep
        shutil.rmtree(d)


def test_archive_and_setup():
    """Done lines leave the board for the archive; setup writes config and skeleton."""
    global TODO, ARCHIVE, VAULT, CONFIG_FILE
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    keep = (TODO, ARCHIVE, VAULT, CONFIG_FILE, dict(CONFIG))
    TODO = CONFIG["todo"] = d / "Todo.md"
    ARCHIVE = CONFIG["archive"] = d / "90-Meta" / "Todo-Archive.md"
    VAULT = CONFIG["vault"] = d
    CONFIG_FILE = d / "handbook.json"
    try:
        save_setup({"owner": ["Sam"], "sections": ["today", "tasks", "bogus"],
                    "projects": ["Site\nApp"], "repos": ["me/site\nnot a repo"]})
        cfg = json.loads(CONFIG_FILE.read_text())
        assert cfg["owner"] == "Sam" and cfg["sections"] == ["today", "tasks"], cfg
        assert cfg["repos"] == ["me/site"], cfg
        assert [x["title"] for x in todo_sections()] == ["Clocks", "Site", "App", "Done"]
        assert (d / "90-Meta" / "Questions.md").exists() and (d / "00-Home.md").exists()
        assert [x[0] for x in active_sections()] == ["today", "tasks"]
        TODO.write_text("# Todo\n\n## Site\n\n- [ ] **Open one** - stays · added 09-01\n"
                        "- [x] **Closed one** - goes · added 09-01 · closed from dashboard 09-03\n"
                        "  a continuation line\n\n## Done\n\n- [x] **Old** - also goes\n")
        assert archive_done() == 2
        left = TODO.read_text()
        assert "Open one" in left and "Closed one" not in left and "Old" not in left, left
        arch = ARCHIVE.read_text()
        assert "## Site\n\n- [x] **Closed one**" in arch and "  a continuation line" in arch, arch
        assert "## Done\n\n- [x] **Old**" in arch, arch
        assert archive_done() == 0
        assert "Sam" in setup_page() and "copy" in setup_page()
        keep_int = CONFIG["integrations"]
        CONFIG["integrations"] = []
        assert m_build({"build": {"review": [], "mine": [], "shipped": 0}}) == ""
        assert m_linear({}) == ""
        CONFIG["integrations"] = keep_int
        deals = [{"stage": "Won"}, {"stage": "Lost"}, {"stage": "Lead"}]
        assert sum(1 for d in deals if d["stage"].startswith(("Won", "Lost"))) == 2
    finally:
        TODO, ARCHIVE, VAULT, CONFIG_FILE, cfg = keep
        CONFIG.clear()
        CONFIG.update(cfg)
        _cache.clear()
        shutil.rmtree(d)


def test_config_overlay():
    """handbook.json wins over the defaults; relative paths sit in the vault."""
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    keep = dict(CONFIG)
    (d / "handbook.json").write_text(json.dumps({
        "owner": "Sam", "todo": "Tasks.md", "repos_git": ["~/x"],
        "hide_dirs": ["n"], "port": 5}))
    try:
        load_config(d / "handbook.json")
        assert CONFIG["owner"] == "Sam" and CONFIG["port"] == 5
        assert CONFIG["todo"] == _VAULT / "Tasks.md", CONFIG["todo"]
        assert CONFIG["repos_git"] == [pathlib.Path.home() / "x"]
        assert CONFIG["hide_dirs"] == {"n"}
        assert "Sam" in greeting()
    finally:
        CONFIG.clear()
        CONFIG.update(keep)
        shutil.rmtree(d)


def test_starter_vault():
    """The bundled starter renders every tab with no real data behind it."""
    global TODO, MEMORY, QUESTIONS, META, VAULT, CAPTURE_LOG
    starter = _HERE / "starter"
    if not starter.is_dir():
        return
    keep = (TODO, MEMORY, QUESTIONS, META, VAULT, CAPTURE_LOG,
            {k: CONFIG[k] for k in ("todo", "memory", "questions", "meta",
                                    "vault", "capture_log", "repos_git")})
    TODO = CONFIG["todo"] = starter / "Todo.md"
    META = CONFIG["meta"] = starter / "90-Meta"
    QUESTIONS = CONFIG["questions"] = META / "Questions.md"
    MEMORY = CONFIG["memory"] = META / "Assistant-Memory.md"
    CAPTURE_LOG = CONFIG["capture_log"] = META / "Capture-Log.md"
    VAULT = CONFIG["vault"] = starter
    CONFIG["repos_git"] = []
    _cache.clear()
    try:
        secs = todo_sections()
        assert sum(len(x["items"]) for x in secs) >= 3, secs
        assert open_queue_questions(), "starter has an open question"
        assert focus_picks()[0], "starter has a tier-1 pick"
        for tab in ("today", "tasks", "questions", "inbox"):
            out = fragment(tab)
            assert "<section" in out, tab
    finally:
        TODO, MEMORY, QUESTIONS, META, VAULT, CAPTURE_LOG, cfg = keep
        CONFIG.update(cfg)
        _cache.clear()


def test_watch_stamps():
    """Every watch target's mtime is seen, and a touch shows up as one source."""
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    keep = {k: CONFIG[k] for k in WATCH.values()}
    try:
        for name, key in WATCH.items():
            p = d / f"{name}.md"
            p.write_text("x")
            CONFIG[key] = p
        first = watch_stamp()
        assert set(first) == set(WATCH) | {"commits"}, first
        assert all(first[n] for n in WATCH), first
        time.sleep(0.02)
        (d / "todo.md").write_text("y")
        second = watch_stamp()
        moved = [k for k in second if first[k] != second[k]]
        assert moved == ["todo"], moved
        CONFIG["todo"] = d / "gone.md"
        assert watch_stamp()["todo"] == 0, "a missing target reads as zero"
        assert git_head_stamp() > 0 or not (VAULT / ".git").exists(), "the vault HEAD must stamp"
    finally:
        CONFIG.update(keep)
        shutil.rmtree(d)


def test_fragments():
    """A fragment is a bare pane, never the shell."""
    for name in ("today", "tasks", "inbox", "rail"):
        frag = fragment(name)
        assert frag, name
        assert "<html" not in frag and "<!doctype" not in frag.lower(), name
        assert 'class="rail"' not in frag and 'class="shell"' not in frag, name
    rail = fragment("rail")
    assert ('class="runs"' in rail or CONFIG.get("runs") == {}) and 'class="keys"' in rail, rail[:200]
    assert 'data-t="today"' in rail, rail[:200]
    hb = fragment("handbook", {"page": "", "changes": False, "commit": "",
                               "graph": True, "repo": "all",
                               "show_repo": CONFIG["wordmark"]})
    assert 'id="vgraph"' in hb and "<html" not in hb, hb[:200]
    assert fragment("nope") == ""


def test_linear_panel():
    """No key file leaves the empty state alone; a key file renders rows."""
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    keep = CONFIG["linear_key_path"]
    keep_int = CONFIG["integrations"]
    CONFIG["integrations"] = ["linear"]
    CONFIG["linear_mine"] = False
    try:
        CONFIG["linear_key_path"] = d / "absent"
        assert "no key was found" in m_linear({}), "no key means the hint"
        CONFIG["linear_key_path"] = d / "linear-api-key"
        CONFIG["linear_key_path"].write_text("lin_api_FAKEKEY\n")
        with _lock:
            _cache["linear"] = (time.time(), [
                {"identifier": "ALX-12", "title": "Wire the panel",
                 "state": {"name": "In Progress"},
                 "url": "https://linear.app/x/issue/ALX-12"}])
        out = m_linear({})
        assert "ALX-12" in out and "Wire the panel" in out, out
        assert "In Progress" in out and "linear.app/x/issue/ALX-12" in out, out
        assert "FAKEKEY" not in out, "the key never reaches the page"
        with _lock:
            _cache["linear"][1][0]["project"] = {"name": "ACME / Northwind"}
        global TODO
        todo = d / "Todo.md"
        todo.write_text("## ACME\n\n- [ ] **Call the bank** · added 08-20\n\n"
                        "## Personal\n\n- [ ] **Dentist** · added 08-22\n")
        keep_todo, TODO = (TODO, CONFIG["todo"]), todo
        CONFIG["todo"] = todo
        try:
            secs = [x for x in todo_sections() if x["items"]]
            work = m_work({"items": [t for x in secs for t in x["items"]], "sections": secs})
        finally:
            TODO, CONFIG["todo"] = keep_todo
        assert work.count('class="pblock" data-p="ACME"') == 1, "the board folds into the team of the same name"
        assert work.index("ALX-12") < work.index("Call the bank") < work.index("Dentist"), work
        assert 'data-p="Personal"' in work and "your board" not in work, work
        with _lock:
            _cache["linear"] = (time.time(), {"error": "Linear answered 401."})
        assert "Linear answered 401." in m_linear({})
    finally:
        with _lock:
            _cache.pop("linear", None)
        CONFIG["linear_key_path"] = keep
        CONFIG["integrations"] = keep_int
        CONFIG["linear_mine"] = True
        shutil.rmtree(d)


def test_sse_cap():
    """Five listeners leave four, and the dropped one is told to close."""
    keep = list(_sse)
    del _sse[:]
    try:
        streams = []
        for _ in range(SSE_MAX + 1):
            q = queue.Queue(maxsize=8)
            streams.append(q)
            sse_add(q)
        assert len(_sse) == SSE_MAX, len(_sse)
        assert streams[0] not in _sse, "the oldest is dropped"
        assert streams[0].get_nowait() is None, "the dropped stream is closed"
        broadcast("todo")
        assert streams[-1].get_nowait() == "todo"
        assert streams[0].empty(), "a dropped stream stops receiving"
        sse_drop(streams[-1])
        assert streams[-1] not in _sse
    finally:
        del _sse[:]
        _sse.extend(keep)


def test_intake_status():
    """Only `status: active` intake forms feed the questions tab and the counts."""
    global META
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "Founder-Intake-2026-08-27.md").write_text(
        "---\ntitle: old\nstatus: resolved\n---\n\n## S\n\n1. A resolved question\n")
    (d / "Founder-Intake-2026-08-29.md").write_text(
        "---\ntitle: new\nstatus: active\n---\n\n## S\n\n"
        "1. A live question\n\n2. An answered one\n\n"
        "> **A (2026-08-29 10:00):** yes\n")
    (d / "Founder-Intake-nofront.md").write_text("# no frontmatter\n\n1. Ignore me\n")
    keep = META
    META = CONFIG["meta"] = d
    try:
        names = [f.name for f in intake_files()]
        assert names == ["Founder-Intake-2026-08-29.md"], names
        qs = intake_questions(intake_files()[0])
        assert [q["answered"] for q in qs] == [False, True], qs
        assert sum(1 for f in intake_files() for q in intake_questions(f)
                   if not q["answered"]) == 1
    finally:
        META = CONFIG["meta"] = keep
        shutil.rmtree(d)


def test_next_up():
    """Next up = bench (max 3) topped to five, heuristic picks labelled on deck."""
    global TODO
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    todo = d / "Todo.md"
    todo.write_text(
        "## ACME\n\n"
        + "".join(f"- [ ] **Task number {i} for the board** - body · added 08-{i:02d}\n"
                  for i in range(10, 20)))
    keep = (TODO, CONFIG["todo"])
    TODO = CONFIG["todo"] = todo
    try:
        board = [t for s in todo_sections() for t in s["items"]]
        c = {"items": board, "all_items": board,
             "clocks": [{"project": "ACME", "what": "", "left": 1, "due": None,
                         "open": 1}]}
        used = {board[0]["id"], board[1]["id"]}
        deck = on_deck(c, used, 2)
        assert len(deck) == 2, deck
        assert all(t["id"] not in used for t, _ in deck), "focus picks stay excluded"
        assert all(w == "on deck · has a clock" for _, w in deck), deck
        assert on_deck(c, used, 0) == []
        c["clocks"] = []
        deck = on_deck(c, set(), 3)
        assert [w for _, w in deck] == ["on deck · oldest sitting"] * 3, deck
        ages = [t["age"] for t, _ in deck]
        assert ages == sorted(ages, reverse=True), ages
        assert len(on_deck(c, set(), 99)) == len(board), "never invents rows"
        html_row = task_row(deck[0][0], "today", "all", deck[0][1], "", "", "ACME", "dk")
        assert 'class="sub dk">on deck · oldest sitting' in html_row, html_row
    finally:
        TODO, CONFIG["todo"] = keep
        shutil.rmtree(d)


def test_group_toggle():
    """Board groups collapse, and a title tagged with its own group drops the tag."""
    global TODO
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    todo = d / "Todo.md"
    todo.write_text(
        "## ACME\n\n"
        "- [ ] **ACME · Smoke-test the signup flow** - before the demo · added 08-20\n"
        "- [ ] **Rewrite the onboarding copy** - the hero line · added 08-22\n")
    keep = (TODO, CONFIG["todo"])
    TODO = CONFIG["todo"] = todo
    try:
        secs = [s for s in todo_sections() if s["items"]]
        board = [t for s in secs for t in s["items"]]
        out = m_work({"items": board, "sections": secs})
        assert 'class="pblock" data-p="ACME"' in out, out
        assert 'class="pill" data-p="ACME"' in out and 'class="gsort"' in out, out
        assert 'class="group gtog" aria-expanded="true"' in out, out
        assert 'onclick="togGroup(this)"' in out and 'class="chev"' in out, out
        assert '<span class="t">Smoke-test the signup flow</span>' in out, out
        assert ">ACME · Smoke-test the signup flow<" not in out, "the group says ACME"
        assert '<span class="t">Rewrite the onboarding copy</span>' in out, out
        tagged = next(t for t in board if t["title"].startswith("ACME · "))
        plain = next(t for t in board if t["title"].startswith("Rewrite"))
        nxt = task_row(tagged, "today", "all", "on deck", "", "", "ACME", "dk")
        assert '<span class="t">ACME · Smoke-test the signup flow</span>' in nxt, nxt
        assert 'class="pfx"' not in nxt, "a tagged title carries its own prefix"
        assert 'class="pfx">ACME · ' in task_row(plain, "today", "all", "", "", "",
                                                "ACME", "dk"), "untagged rows keep it"
        assert strip_tag("ACME · x", "ACME — client build, week 5 of 8") == "x"
        assert strip_tag("ACMEX · x", "ACME") == "ACMEX · x", "exact tags only"
        assert strip_tag("ACME - x", "ACME") == "ACME - x", "the separator is exact"
        assert strip_tag("x", "ACME") == "x"
        assert "GKEY='hbgroups'" in JS and "restoreGroups(el)" in JS
    finally:
        TODO, CONFIG["todo"] = keep
        shutil.rmtree(d)


def test_bench_promotion():
    """Closing a focus pick refills the strip, and never touches the memory file."""
    global TODO, MEMORY
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    todo, mem = d / "Todo.md", d / "Assistant-Memory.md"
    todo.write_text(
        "## ACME\n\n"
        "- [ ] **Rewrite the first onboarding screen copy** - the hero line · added 08-28\n"
        "- [ ] **Record the Loom videos for Monday** - for Beta · added 08-26\n")
    mem.write_text(
        "## Tier-1 carry\n\n"
        "- Record the Loom videos for Monday - 2d *(clocked out loud)*\n\n"
        "### Bench\n\n"
        "1. Rewrite the first onboarding screen copy *(ACME; reminder 08-28)*\n")
    before = mem.read_text()
    keep = (TODO, MEMORY, CONFIG["todo"], CONFIG["memory"])
    TODO, MEMORY, CONFIG["todo"], CONFIG["memory"] = todo, mem, todo, mem
    try:
        picks, bench = focus_picks()
        assert [p["day"] for p in picks] == [2], picks
        assert bench == ["Rewrite the first onboarding screen copy"], bench
        tid = next(t["id"] for s in todo_sections() for t in s["items"]
                   if t["title"].startswith("Record"))
        assert close_task(tid, "done")
        assert "closed from dashboard" in todo.read_text()
        closed = next(t for s in todo_sections() for t in s["items"] if t["done"])
        focus = task_row(closed, "today", "all", "", "day 2", day_title(2))
        assert 'class="row done"' in focus, focus
        assert 'name="action" value="reopen"' in focus, focus
        assert "second day as a tier-1 pick" in focus, focus
        row = promoted_row()
        assert "Rewrite the first onboarding screen copy" in row, row
        assert 'class="box"' in row, row
        assert 'class="pfx">ACME' in row, row
        assert mem.read_text() == before
    finally:
        TODO, MEMORY, CONFIG["todo"], CONFIG["memory"] = keep
        shutil.rmtree(d)


def warm():
    context()
    repos = git_repos()
    for key, path in repos.items():
        repo_log(key, path)
    cached("graph", 120, vault_graph)


if __name__ == "__main__":
    if "--check" in sys.argv:
        selftest()
    else:
        print(f"handbook: vault {VAULT} on http://127.0.0.1:{PORT}", flush=True)
        threading.Thread(target=warm, daemon=True).start()
        threading.Thread(target=watcher, daemon=True).start()
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
