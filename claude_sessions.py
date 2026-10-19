#!/usr/bin/env python3
"""Snapshot and restore iTerm2 tabs/splits + the Claude Code sessions running in them.

Subcommands:
  snapshot   Record every iTerm2 pane (cwd, profile, position) and any interactive
             Claude Code session running in it. Run every minute by launchd.
  recover    Forensic fallback when no snapshot exists: infer which sessions were open
             before the last shutdown from transcript mtimes (live processes touch
             their transcript every ~16 min) and write a snapshot-format file.
  show       Pretty-print a snapshot (default: last one taken before this boot).
  restore    Recreate windows/tabs/splits in iTerm2 and `claude --resume` each session.

Works with or without claude-auto-retry/tmux: pane identity comes from the
ITERM_SESSION_ID env var that every process in an iTerm2 pane inherits.
Compatible with /usr/bin/python3 (3.9).
"""
import argparse, datetime as dt, glob, hashlib, json, os, re, shlex, subprocess, sys, time

HOME = os.path.expanduser("~")
# Every Claude Code config dir (~/.claude plus any per-project CLAUDE_CONFIG_DIR like ~/.claude-work).
CONFIG_DIRS = sorted(d for d in glob.glob(os.path.join(HOME, ".claude*")) if os.path.isdir(os.path.join(d, "projects")))
DEFAULT_CONFIG = os.path.join(HOME, ".claude")
STATE_DIR = os.path.join(HOME, ".local", "state", "claude-session-restore")
SNAP_DIR = os.path.join(STATE_DIR, "snapshots")
PANE_DIR = os.path.join(STATE_DIR, "panes")
HERE = os.path.dirname(os.path.realpath(__file__))
TAB_RULES_FILE = os.path.join(HERE, "tabs.json")
# Personal settings (git-ignored), e.g. {"skip_permissions_alias": "cld", "default_flags": [...]}
try:
    LOCAL = json.load(open(os.path.join(HERE, "local.json")))
except (OSError, ValueError):
    LOCAL = {}
# History pruning: drop files older than KEEP_DAYS and beyond MAX_FILES, but never the newest
# KEEP_MIN, so a layout that stays unchanged for weeks still has a snapshot to restore from.
KEEP_DAYS, KEEP_MIN, MAX_FILES = 14, 50, 1000
# Flags used when a recovered session has no recorded argv.
DEFAULT_FLAGS = LOCAL.get("default_flags", ["--dangerously-skip-permissions"])
# Flags that select/name a session; stripped from recorded argv before adding --resume.
SESSION_FLAGS_WITH_VALUE = {"--resume", "-r", "--session-id", "--name", "-n", "--from-pr", "--teleport"}
SESSION_FLAGS_BARE = {"--continue", "-c", "--fork-session"}
ITERM_ID_RE = re.compile(r"\bITERM_SESSION_ID=w(\d+)t(\d+)p(\d+):([0-9A-F-]{36})")
ITERM_PROFILE_RE = re.compile(r"\bITERM_PROFILE=(.+?)(?= [A-Za-z_][A-Za-z0-9_]*=|$)")


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def boot_time():
    m = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"]))
    return int(m.group(1)) if m else 0


def encode_project(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


# ---------------------------------------------------------------- live discovery

def process_table():
    procs = {}
    for line in sh(["ps", "-axww", "-o", "pid=,ppid=,comm="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            procs[int(parts[0])] = {"ppid": int(parts[1]), "comm": parts[2]}
    return procs


def env_of(pids):
    """Map pid -> (window, tab, pane, uuid, profile) from ITERM_* env vars."""
    out = {}
    if not pids:
        return out
    for pid in pids:
        line = sh(["ps", "eww", "-o", "command=", "-p", str(pid)])
        m = ITERM_ID_RE.search(line)
        if not m:
            continue
        p = ITERM_PROFILE_RE.search(line)
        out[pid] = (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), p.group(1) if p else None)
    return out


def cwds_of(pids):
    out = {}
    if not pids:
        return out
    pid = None
    for line in sh(["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-Fpn"]).splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            out[pid] = line[1:]
    return out


def argv_of(pid):
    return shlex.split(sh(["ps", "-ww", "-o", "args=", "-p", str(pid)]).strip() or "x")[1:]


def live_claude_sessions(procs):
    """Interactive Claude Code processes from each config dir's sessions/<pid>.json registry."""
    found = []
    for cfg in CONFIG_DIRS:
        for f in glob.glob(os.path.join(cfg, "sessions", "*.json")):
            try:
                reg = json.load(open(f))
            except Exception:
                continue
            pid = reg.get("pid")
            if pid not in procs or "claude" not in procs[pid]["comm"]:
                continue  # stale entry (process gone)
            if reg.get("kind") != "interactive" or reg.get("entrypoint") not in (None, "cli"):
                continue
            argv = argv_of(pid)
            if "remote-control" in argv:
                continue  # always-on claude-rc servers are managed by their own LaunchAgents
            found.append({"pid": pid, "session_id": reg["sessionId"], "config_dir": cfg,
                          "cwd": reg.get("cwd"), "name": reg.get("name"), "status": reg.get("status"),
                          "started_at": reg.get("startedAt"), "tmux": reg.get("tmux"), "args": argv})
    return found


def recorded_shells(procs):
    """Panes self-reported by pane-hook.zsh: {pid: (w, t, p, uuid, profile)} plus pid -> cwd.
    (macOS hides login-shell environments from `ps eww`, so we can't read ITERM_SESSION_ID directly.)"""
    envs, cwds = {}, {}
    for f in glob.glob(os.path.join(PANE_DIR, "*")):
        try:
            pid, sid, prof, cwd = open(f).read().rstrip("\n").split("\t", 3)
            pid = int(pid)
        except Exception:
            continue
        m = ITERM_ID_RE.search("ITERM_SESSION_ID=" + sid)
        if pid not in procs or not m:
            os.remove(f)  # shell exited (or pane closed)
            continue
        envs[pid] = (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), prof or None)
        cwds[pid] = cwd
    return envs, cwds


ITERM_STATE_SCRIPT = '''
set US to character id 31
set out to ""
tell application "iTerm2"
  set wi to 0
  repeat with w in windows
    set wi to wi + 1
    set ti to 0
    repeat with t in tabs of w
      set ti to ti + 1
      set pi to 0
      repeat with s in sessions of t
        set pi to pi + 1
        tell s
          set out to out & wi & US & (id of w) & US & ti & US & (variable named "tab.titleOverride") & US & ¬
            (variable named "tab.title") & US & pi & US & (unique id) & US & (tty) & US & (variable named "path") & US & ¬
            (profile name) & US & (columns) & US & (rows) & US & (is at shell prompt) & linefeed
        end tell
      end repeat
    end repeat
  end repeat
end tell
return out
'''
ITERM_FIELDS = ["window", "window_id", "tab", "tab_title", "auto_title", "pane", "iterm_uuid", "tty", "path",
                "profile", "columns", "rows", "at_prompt"]


def iterm_state():
    """Every iTerm2 pane via AppleScript (works from launchd without prompts), or None if unavailable."""
    try:
        r = subprocess.run(["osascript", "-"], input=ITERM_STATE_SCRIPT, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode:
        return None
    rows = []
    for line in r.stdout.splitlines():
        f = line.split("\x1f")
        if len(f) != len(ITERM_FIELDS):
            continue
        d = dict(zip(ITERM_FIELDS, f))
        for k in ("window", "window_id", "tab", "pane", "columns", "rows"):
            d[k] = int(d[k]) - (1 if k in ("window", "tab", "pane") else 0)
        d["at_prompt"] = d["at_prompt"] == "true"
        d["tab_title"] = d["tab_title"] or None
        rows.append(d)
    return rows


VENV_PY = os.path.join(HERE, ".venv", "bin", "python")
LAYOUT_PY = os.path.join(HERE, "iterm_layout.py")


def capture_layout():
    """Exact windows/tabs/split trees via the iTerm2 Python API, or None if unavailable."""
    try:
        r = subprocess.run([VENV_PY, LAYOUT_PY, "capture"], capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


def layout_leaves(node):
    if "children" in node:
        return [l for c in node["children"] for l in layout_leaves(c)]
    return [node]


def take_snapshot():
    procs = process_table()
    claudes = live_claude_sessions(procs)
    layout = capture_layout()
    if layout and layout.get("windows"):
        return snapshot_from_layout(layout, claudes)
    state = iterm_state()
    if state is not None:
        return snapshot_from_iterm(state, claudes)
    return snapshot_from_env(procs, claudes)


def claude_ttys(claudes):
    """pid -> the iTerm2 pane tty showing that claude: its tmux client's tty when wrapped by
    claude-auto-retry (a re-attached session keeps its original ITERM_SESSION_ID env, so env
    can't be trusted), else the process's own tty."""
    clients = {}
    for line in sh(["tmux", "list-clients", "-F", "#{session_name}\t#{client_tty}"]).splitlines():
        name, _, tty = line.partition("\t")
        clients.setdefault(name, tty)
    out = {}
    for c in claudes:
        tmux_session = (c.get("tmux") or "").split(":")[0]
        if tmux_session:
            if tmux_session in clients:
                out[c["pid"]] = clients[tmux_session]
            continue  # detached: not visible in any pane
        tty = sh(["ps", "-o", "tty=", "-p", str(c["pid"])]).strip()
        if tty and tty != "??":
            out[c["pid"]] = "/dev/" + tty
    return out


def claude_by_tty(claudes):
    ttys = claude_ttys(claudes)
    by_tty = {}
    for c in sorted(claudes, key=lambda c: c.get("started_at") or 0):
        t = ttys.get(c["pid"])
        if t and t not in by_tty:  # outermost (earliest) claude in the pane wins
            by_tty[t] = c
    return by_tty


def snapshot_from_layout(layout, claudes):
    by_tty = claude_by_tty(claudes)
    panes = []
    for wi, w in enumerate(layout["windows"]):
        for ti, t in enumerate(w["tabs"]):
            for pi, leaf in enumerate(layout_leaves(t["root"])):
                panes.append({"window": wi, "tab": ti, "pane": pi, "tab_title": t["title"],
                              "iterm_uuid": leaf["session_id"], "profile": leaf.get("profile"),
                              "cwd": leaf.get("path") or None, "columns": leaf.get("cols"), "rows": leaf.get("rows"),
                              "claude": by_tty.get(leaf.get("tty"))})
    return {"taken_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "boot_time": boot_time(), "source": "snapshot-layout", "layout": layout, "panes": panes}


def snapshot_from_iterm(state, claudes):
    ttys = claude_ttys(claudes)
    by_tty = {}
    for c in sorted(claudes, key=lambda c: c.get("started_at") or 0):
        t = ttys.get(c["pid"])
        if t and t not in by_tty:  # outermost (earliest) claude in the pane wins
            by_tty[t] = c
    panes = []
    for s in state:
        panes.append({"window": s["window"], "tab": s["tab"], "pane": s["pane"], "tab_title": s["tab_title"],
                      "iterm_uuid": s["iterm_uuid"], "profile": s["profile"], "cwd": s["path"] or None,
                      "columns": s["columns"], "rows": s["rows"], "claude": by_tty.get(s["tty"])})
    return {"taken_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "boot_time": boot_time(), "source": "snapshot-iterm", "panes": panes}


def snapshot_from_env(procs, claudes):
    """Fallback when AppleScript is unavailable: positions from ITERM_SESSION_ID, no tab titles."""
    envs, cwds = recorded_shells(procs)
    shells = list(envs)
    live_cwd = cwds_of(shells)  # prefer the real cwd if it changed since the last prompt
    envs.update(env_of([c["pid"] for c in claudes]))
    panes = {}
    for pid in shells:
        w, t, p, uuid, prof = envs[pid]
        panes[uuid] = {"window": w, "tab": t, "pane": p, "iterm_uuid": uuid, "profile": prof,
                       "cwd": live_cwd.get(pid) or cwds.get(pid), "claude": None}
    for c in sorted(claudes, key=lambda c: c.get("started_at") or 0):
        if c["pid"] not in envs:
            continue  # not running inside iTerm2 (VS Code, cron, ...)
        w, t, p, uuid, prof = envs[c["pid"]]
        pane = panes.setdefault(uuid, {"window": w, "tab": t, "pane": p, "iterm_uuid": uuid,
                                       "profile": prof, "cwd": c["cwd"], "claude": None})
        if pane["claude"] is None:  # outermost (earliest) claude in the pane wins
            pane["claude"] = c
    return {"taken_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "boot_time": boot_time(), "source": "snapshot",
            "panes": sorted(panes.values(), key=lambda p: (p["window"], p["tab"], p["pane"]))}


def prune(directory, companions=()):
    """Delete *.json (and same-named companion files) older than KEEP_DAYS or beyond MAX_FILES,
    always keeping the newest KEEP_MIN."""
    files = sorted(glob.glob(os.path.join(directory, "*.json")), key=os.path.getmtime, reverse=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    for i, f in enumerate(files):
        if i >= KEEP_MIN and (i >= MAX_FILES or os.path.getmtime(f) < cutoff):
            for path in [f] + [f[:-5] + ext for ext in companions]:
                try:
                    os.remove(path)
                except OSError:
                    pass


FREEZE_FILE = os.path.join(STATE_DIR, "frozen.json")


def freeze_active():
    """`prepare` freezes the snapshot it took so windows closing during shutdown can't replace it.
    The freeze lapses after its timeout (you didn't reboot after all) or once a new boot happens."""
    try:
        f = json.load(open(FREEZE_FILE))
    except (OSError, ValueError):
        return None
    if f.get("boot_time") != boot_time() or time.time() > f.get("until", 0):
        os.remove(FREEZE_FILE)
        return None
    return f


def cmd_snapshot(a):
    if a.auto and freeze_active():
        return  # background run during a prepare freeze
    snap = take_snapshot()
    os.makedirs(SNAP_DIR, exist_ok=True)
    # Only write when the layout/sessions changed, so history stays meaningful.
    key = json.dumps([[p["iterm_uuid"], p.get("tab_title"), p["cwd"], p.get("columns"), p.get("rows"),
                       (p["claude"] or {}).get("session_id")] for p in snap["panes"]]
                     + [w.get("frame") for w in (snap.get("layout") or {}).get("windows", [])])
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    last_digest_file = os.path.join(STATE_DIR, "last-digest")
    prev = open(last_digest_file).read().strip() if os.path.exists(last_digest_file) else ""
    snap["digest"] = digest
    if not snap["panes"]:
        return  # iTerm2 not running (e.g. just after login) - never overwrite with nothing
    latest = os.path.join(STATE_DIR, "latest.json")
    with open(latest + ".tmp", "w") as f:
        json.dump(snap, f, indent=1)
    os.replace(latest + ".tmp", latest)
    if digest != prev or a.force:
        name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
        with open(os.path.join(SNAP_DIR, name), "w") as f:
            json.dump(snap, f, indent=1)
        open(last_digest_file, "w").write(digest)
    a.written = latest
    prune(SNAP_DIR)
    prune(os.path.join(STATE_DIR, "firefox"), companions=(".html",))
    if not a.quiet:
        n = sum(1 for p in snap["panes"] if p["claude"])
        print(f"snapshot: {len(snap['panes'])} panes, {n} claude sessions -> {latest}")


# ---------------------------------------------------------------- transcripts

def find_transcript(session_id, config_dir=None):
    dirs = [config_dir] if config_dir else CONFIG_DIRS
    for cfg in dirs:
        hits = glob.glob(os.path.join(cfg, "projects", "*", session_id + ".jsonl"))
        if hits:
            return cfg, hits[0]
    return None, None


def transcript_info(path):
    """Title, launch cwd (matching the project folder) and last activity of a transcript."""
    folder = os.path.basename(os.path.dirname(path))
    title = ai_title = last_prompt = None
    cwds, last_ts, cwd_counts = [], None, {}
    with open(path, errors="replace") as f:
        for line in f:
            if '"custom-title"' not in line and '"ai-title"' not in line and '"cwd"' not in line \
                    and '"last-prompt"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "custom-title":
                title = o.get("customTitle") or title
            elif t == "ai-title":
                ai_title = o.get("aiTitle") or ai_title
            elif t == "last-prompt":
                last_prompt = o.get("lastPrompt") or last_prompt
            if o.get("cwd"):
                if o["cwd"] not in cwds:
                    cwds.append(o["cwd"])
                cwd_counts[o["cwd"]] = cwd_counts.get(o["cwd"], 0) + 1
            if t in ("user", "assistant") and o.get("timestamp"):
                last_ts = o["timestamp"]
    project_cwd = next((c for c in cwds if encode_project(c) == folder), cwds[0] if cwds else None)
    work_cwd = max(cwd_counts, key=cwd_counts.get) if cwd_counts else project_cwd
    return {"title": title or ai_title, "project_cwd": project_cwd, "work_cwd": work_cwd,
            "last_message_at": last_ts, "last_prompt": last_prompt}


def cmd_recover(a):
    """Infer what was open before the last shutdown from transcript mtime 'heartbeats'."""
    bt = boot_time()
    files = []
    for cfg in CONFIG_DIRS:
        for p in glob.glob(os.path.join(cfg, "projects", "*", "*.jsonl")):
            m = os.path.getmtime(p)
            if m < bt and os.path.getsize(p) > 0:
                files.append((m, cfg, p))
    if not files:
        sys.exit("no transcripts found from before this boot")
    end = dt.datetime.fromisoformat(a.until).timestamp() if a.until else max(f[0] for f in files)
    start = end - a.window * 60
    picked = sorted([f for f in files if start <= f[0] <= end], reverse=True)
    if a.ids:  # explicit session ids (e.g. ones resumed since, whose mtimes have moved on)
        picked = []
        for sid in a.ids:
            cfg, path = find_transcript(sid) if len(sid) == 36 else next(
                ((c, q) for c in CONFIG_DIRS for q in glob.glob(os.path.join(c, "projects", "*", sid + "*.jsonl"))), (None, None))
            if path:
                picked.append((os.path.getmtime(path), cfg, path))
            else:
                print(f"warn: no transcript for {sid}")
    panes = []
    for i, (m, cfg, p) in enumerate(picked):
        sid = os.path.basename(p)[:-6]
        info = transcript_info(p)
        panes.append({"window": None, "tab": None, "pane": i, "iterm_uuid": None, "profile": None,
                      "cwd": info["project_cwd"], "enabled": True,
                      "claude": {"session_id": sid, "config_dir": cfg, "cwd": info["project_cwd"], "args": None,
                                 "title": info["title"], "work_cwd": info["work_cwd"],
                                 "last_message_at": info["last_message_at"],
                                 "transcript_mtime": dt.datetime.fromtimestamp(m).isoformat(timespec="seconds")}})
    rules = load_rules()
    for p in panes:
        p["tab_title"] = tab_for(p, rules)
    snap = {"taken_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "boot_time": bt,
            "source": "recover", "window": [dt.datetime.fromtimestamp(start).isoformat(timespec="seconds"),
                                            dt.datetime.fromtimestamp(end).isoformat(timespec="seconds")],
            "panes": panes}
    out = a.output or os.path.join(STATE_DIR, "recovered-" + dt.datetime.fromtimestamp(end).strftime("%Y%m%d-%H%M") + ".json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(snap, f, indent=1)
    print(f"recovered {len(panes)} sessions touched {snap['window'][0]} .. {snap['window'][1]}")
    print(f"wrote {out}\n  edit \"tab_title\" / set \"enabled\": false as needed, then: claude-restore {out}")
    show(snap)


# ---------------------------------------------------------------- show / restore

def load_snapshot(path):
    if path:
        return json.load(open(path))
    # Default: the newest snapshot taken BEFORE this boot, i.e. what you had open last time.
    bt = boot_time()
    snaps = sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")))
    before = [s for s in snaps if json.load(open(s)).get("boot_time", 0) < bt]
    if not before:
        sys.exit("no snapshot from before this boot; try `recover` or pass a file")
    print(f"using {before[-1]}")
    return json.load(open(before[-1]))


def enrich(snap):
    for p in snap["panes"]:
        c = p.get("claude")
        if c and not c.get("title"):
            _, path = find_transcript(c["session_id"], c.get("config_dir"))
            if path:
                info = transcript_info(path)
                c["title"] = info["title"]
                c.setdefault("project_cwd", info["project_cwd"])
                c.setdefault("work_cwd", info["work_cwd"])
    return snap


def show(snap):
    enrich(snap)
    rules = load_rules()
    print(f"\nsnapshot {snap.get('taken_at')} ({snap.get('source')}), {len(snap['panes'])} panes")
    panes = sorted(snap["panes"], key=lambda p: ((p.get("window") or 0), (p.get("tab") or 0), p.get("pane") or 0))
    by_tab = {}
    for p in panes:
        by_tab.setdefault((p.get("window") or 0, tab_for(p, rules)), []).append(p)
    for (w, title), items in by_tab.items():
        print(f"  [{title}]" + (f"  (window {w + 1})" if w else ""))
        for p in items:
            c = p.get("claude")
            flag = "" if p.get("enabled", True) else "  [disabled]"
            if c:
                print(f"     {c.get('title') or '(untitled)'}  [{os.path.basename(c['config_dir'])}]{flag}")
                print(f"        {resume_dir(p)}  --resume {c['session_id']}")
            else:
                print(f"     shell  {p.get('cwd')}{flag}")


def load_rules():
    try:
        rules = json.load(open(TAB_RULES_FILE))
    except FileNotFoundError:
        rules = []
    for r in rules:
        for k in ("config_dir", "cwd_prefix"):
            if k in r:
                r[k] = os.path.expanduser(r[k]).rstrip("/")
    return rules


def tab_for(p, rules):
    """Recorded tab title, else the first matching rule in tabs.json, else "Other"."""
    if p.get("tab_title"):
        return p["tab_title"]
    c = p.get("claude") or {}
    cwds = [d for d in (resume_dir(p), c.get("cwd"), c.get("work_cwd"), p.get("cwd")) if d]
    for r in rules:
        if "config_dir" in r and r["config_dir"] != (c.get("config_dir") or "").rstrip("/"):
            continue
        if "cwd_prefix" in r and not any(d.startswith(r["cwd_prefix"]) for d in cwds):
            continue
        if "session" in r and not c.get("session_id", "").startswith(r["session"]):
            continue
        return r["title"]
    return "Other"


def rule_profile(title, rules):
    return next((r["profile"] for r in rules if r.get("title") == title and r.get("profile")), None)


def resume_dir(p):
    c = p.get("claude")
    if c:
        return c.get("project_cwd") or c.get("cwd") or p.get("cwd")
    return p.get("cwd")


def claude_command(c):
    args = list(c.get("args") or DEFAULT_FLAGS)
    clean, skip = [], False
    for x in args:
        if skip:
            skip = False
            continue
        flag = x.split("=", 1)[0]
        if flag in SESSION_FLAGS_WITH_VALUE:
            skip = "=" not in x
            continue
        if flag in SESSION_FLAGS_BARE:
            continue
        clean.append(x)
    exe = "claude"
    alias = LOCAL.get("skip_permissions_alias")  # e.g. "cld" = claude --dangerously-skip-permissions
    if alias and "--dangerously-skip-permissions" in clean:
        clean.remove("--dangerously-skip-permissions")
        exe = alias
    cmd = exe + " " + " ".join(shlex.quote(x) for x in clean + ["--resume", c["session_id"]])
    cfg = c["config_dir"]
    if os.path.realpath(cfg) == os.path.realpath(DEFAULT_CONFIG):
        # ~/.claude is the *unset* default; setting it explicitly changes where .claude.json lives.
        return f"(unset CLAUDE_CONFIG_DIR; {cmd})"
    return f"CLAUDE_CONFIG_DIR={shlex.quote(cfg)} {cmd}"


def pane_command(p):
    cmd = "cd " + shlex.quote(resume_dir(p) or HOME)
    c = p.get("claude")
    if c and c.get("attach"):
        return cmd + " && tmux attach-session -t " + shlex.quote(c["attach"])
    if c:
        # One line, chained with &&, so a dropped/garbled keystroke can never launch claude in
        # the wrong dir. direnv is loaded explicitly since its prompt hook hasn't run yet for the
        # new dir (GH_TOKEN etc. come from .envrc).
        cmd += ' && { command -v direnv >/dev/null && eval "$(direnv export zsh 2>/dev/null)"; true; } && ' + claude_command(c)
    return cmd


def running_sessions():
    """session_id -> live registry entry (incl. its claude-auto-retry tmux session, if any)."""
    return {c["session_id"]: c for c in live_claude_sessions(process_table())}


def attached_tmux_sessions():
    out = sh(["tmux", "list-sessions", "-F", "#{session_name} #{session_attached}"])
    return {l.split()[0] for l in out.splitlines() if l.split()[-1] != "0"}


def asq(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def plan_restore(panes, state, rules):
    """Group panes by target tab title; reuse idle panes (at a shell prompt) in same-titled tabs of
    the front window, split when a tab runs out of idle panes, create tabs that don't exist."""
    front = [s for s in state if s["window"] == 0]
    tabs = {}
    for s in front:
        tabs.setdefault(s["tab_title"] or s["auto_title"], []).append(s)
    groups = {}
    for p in panes:
        groups.setdefault(tab_for(p, rules), []).append(p)
    # Existing tabs first (left-to-right), then new tabs in first-seen order.
    order = sorted(groups, key=lambda t: (t not in tabs, min(s["tab"] for s in tabs[t]) if t in tabs else 0))
    plan = []
    for title in order:
        existing = tabs.get(title, [])
        idle = [s for s in existing if s["at_prompt"]]
        for p in groups[title]:
            plan.append({"title": title, "pane": p, "use": idle.pop(0)["iterm_uuid"] if idle else None,
                         "new_tab": not existing, "profile": p.get("profile") or rule_profile(title, rules),
                         "last_existing": existing[-1]["iterm_uuid"] if existing else None})
    return plan, (front[0]["window_id"] if front else None)


def build_applescript(plan, window_id, delay):
    lines = ["on findS(uid)", 'tell application "iTerm2"', "repeat with w in windows", "repeat with t in tabs of w",
             "repeat with s in sessions of t", "if (unique id of s) is uid then return s",
             "end repeat", "end repeat", "end repeat", "end tell", "return missing value", "end findS", "",
             'set newTabs to ""', 'tell application "iTerm2"', "activate"]
    lines.append(f"set w to window id {window_id}" if window_id else "set w to (create window with default profile)")
    last_var, splits = {}, {}
    for i, step in enumerate(plan):
        v, title = f"s{i}", step["title"]
        prof = f"profile {asq(step['profile'])}" if step.get("profile") else "default profile"
        if step["use"]:
            lines.append(f"set {v} to my findS({asq(step['use'])})")
        elif title not in last_var and step["new_tab"]:
            if step.get("profile"):  # fall back to the default profile if that profile was deleted
                lines += ["try", f"tell w to set t to (create tab with {prof})", "on error",
                          "tell w to set t to (create tab with default profile)", "end try"]
            else:
                lines.append(f"tell w to set t to (create tab with {prof})")
            lines.append(f"set {v} to current session of t")
            lines.append(f"set newTabs to newTabs & {asq(title)} & (character id 9) & (unique id of {v}) & linefeed")
        else:
            src = last_var.get(title) or f"(my findS({asq(step['last_existing'])}))"
            n = splits.get(title, 0)
            splits[title] = n + 1
            how = "vertically" if n % 2 == 0 else "horizontally"
            lines.append(f"tell {src} to set {v} to (split {how} with same profile)")
        last_var[title] = v
        p = step["pane"]
        c = p.get("claude")
        lines.append(f"tell {v}")
        if c and c.get("title"):
            lines.append(f"set name to {asq(c['title'][:60])}")
        lines.append(f"write text {asq(pane_command(p))}")
        lines.append("end tell")
        lines.append(f"delay {delay}")
    lines += ["end tell", "return newTabs"]
    return "\n".join(lines)


def set_tab_titles(new_tabs):
    """Tab titles can only be set via the iTerm2 Python API (Settings > General > Magic > Enable Python API)."""
    if not new_tabs:
        return
    helper = os.path.join(HERE, "set_tab_titles.py")
    venv_py = os.path.join(HERE, ".venv", "bin", "python")
    args = [x for title, uid in new_tabs for x in (uid, title)]
    try:
        r = subprocess.run([venv_py, helper] + args, capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
    except Exception:
        ok = False
    pending = os.path.join(STATE_DIR, "pending-tab-titles.json")
    if ok and os.path.exists(pending):
        os.remove(pending)
    if not ok:
        print("note: couldn't set tab titles for new tabs (" + ", ".join(t for t, _ in new_tabs) + ").\n"
              "      Enable iTerm2 > Settings > General > Magic > Enable Python API, then: claude-restore titles")
        json.dump(new_tabs, open(os.path.join(STATE_DIR, "pending-tab-titles.json"), "w"))


def cmd_titles(a):
    path = os.path.join(STATE_DIR, "pending-tab-titles.json")
    if os.path.exists(path):
        set_tab_titles(json.load(open(path)))


def launched_by_claude():
    """True if we're running under a Claude Code process (e.g. its Bash tool) rather than
    directly in a shell; then we must never close the window we're in."""
    pid = os.getppid()
    for _ in range(12):
        line = sh(["ps", "-o", "ppid=,comm=", "-p", str(pid)]).strip()
        if not line:
            return False
        ppid, comm = line.split(None, 1)
        if os.path.basename(comm) == "claude":
            return True
        pid = int(ppid)
        if pid <= 1:
            return False
    return False


def idle_window_ids(current):
    """API window ids whose panes are all idle shells (e.g. the default arrangement iTerm2 opened
    at login). The pane running claude-restore counts as idle if run directly from a shell."""
    state = {s["iterm_uuid"]: s["at_prompt"] for s in (iterm_state() or [])}
    me = os.environ.get("ITERM_SESSION_ID", ":").split(":", 1)[1]
    me_ok = not launched_by_claude()
    out = []
    for w in current.get("windows", []):
        uuids = [l["session_id"] for t in w["tabs"] for l in layout_leaves(t["root"])]
        if uuids and all(state.get(u) or (me_ok and u == me) for u in uuids):
            out.append(w["window_id"])
    return out


def restore_exact(snap, a, live, attached):
    by_uuid = {p["iterm_uuid"]: p for p in snap["panes"]}
    plan = {"windows": [], "delay": a.delay}
    counts = {"resume": 0, "attach": 0, "shell": 0}
    for w in snap["layout"]["windows"]:
        print(f"window {len(plan['windows']) + 1}  frame={w.get('frame')}")
        tabs = []
        for t in w["tabs"]:
            root = json.loads(json.dumps(t["root"]))
            print(f"  [{t.get('title') or '(untitled)'}]")
            for leaf in layout_leaves(root):
                p = by_uuid.get(leaf["session_id"], {})
                c = p.get("claude") if p.get("enabled", True) else None
                leaf["key"] = leaf.pop("session_id")
                for k in ("tty", "auto_name", "path"):
                    leaf.pop(k, None)
                d = resume_dir(p) if p else None
                if d and not os.path.isdir(d):
                    d = HOME
                desc = f"shell {d or '~'}"
                if c and c["session_id"] in live:
                    tmux_session = (live[c["session_id"]].get("tmux") or "").split(":")[0]
                    if tmux_session and tmux_session not in attached:
                        c["attach"] = tmux_session
                    else:
                        desc = f"shell {d}   ({c.get('title')} is already open elsewhere)"
                        c = None
                elif c:
                    c.pop("attach", None)
                if c:
                    if c.get("project_cwd") and not os.path.isdir(c["project_cwd"]):
                        c["project_cwd"] = HOME
                    leaf["command"] = pane_command(p)
                    leaf["name"] = (c.get("title") or "")[:60] or None
                    kind = "attach" if c.get("attach") else "resume"
                    counts[kind] += 1
                    desc = f"{c.get('title') or c['session_id']}  ({'re-attach' if kind == 'attach' else 'resume'})"
                else:
                    leaf["command"] = "cd " + shlex.quote(d) if d else None
                    counts["shell"] += 1
                print(f"     {leaf.get('cols')}x{leaf.get('rows'):<4} {desc}")
            tabs.append({"title": t.get("title"), "root": root})
        plan["windows"].append({"frame": w.get("frame"), "tabs": tabs})
    current = capture_layout() or {"windows": []}
    plan["close_window_ids"] = [] if a.keep_windows else idle_window_ids(current)
    print(f"\n{counts['resume']} to resume, {counts['attach']} to re-attach, {counts['shell']} shells; "
          f"closing {len(plan['close_window_ids'])} idle window(s) afterwards")
    if a.dry_run:
        return
    r = subprocess.run([VENV_PY, LAYOUT_PY, "build"], input=json.dumps(plan), text=True, capture_output=True)
    if r.returncode:
        sys.exit("layout build failed: " + r.stderr[-2000:])
    print("restored.")
    ff_dir = os.path.join(STATE_DIR, "firefox")
    if any(os.path.getmtime(f) < boot_time() for f in glob.glob(os.path.join(ff_dir, "*.json"))):
        print("Firefox tabs from before the reboot are saved: `claude-restore firefox list` / `claude-restore firefox restore`")


def cmd_restore(a):
    snap = enrich(load_snapshot(a.file))
    if snap.get("layout") and not (a.fill or a.only or a.claude_only):
        if not capture_layout():
            sys.exit("iTerm2 Python API unavailable (Settings > General > Magic > Enable Python API); use --fill")
        live, attached = running_sessions(), attached_tmux_sessions()
        return restore_exact(snap, a, live, attached)
    rules = load_rules()
    live = running_sessions()
    attached = attached_tmux_sessions()
    panes = []
    for p in sorted(snap["panes"], key=lambda p: ((p.get("window") or 0), (p.get("tab") or 0), p.get("pane") or 0)):
        c = p.get("claude")
        if not p.get("enabled", True) or (a.claude_only and not c):
            continue
        if c and c["session_id"] in live:
            tmux_session = (live[c["session_id"]].get("tmux") or "").split(":")[0]
            if not tmux_session or tmux_session in attached:
                print(f"skip (already open): {c.get('title')} {c['session_id']}")
                continue
            c["attach"] = tmux_session  # still running, detached (its window was closed): re-attach
        elif c:
            c.pop("attach", None)
        if a.only and not (c and any(c["session_id"].startswith(w) for w in a.only)):
            continue
        d = resume_dir(p)
        if d and not os.path.isdir(d):
            print(f"warn: {d} no longer exists; using {HOME}")
            if c:
                c["project_cwd"] = HOME
            p["cwd"] = HOME
        panes.append(p)
    if not panes:
        print("nothing to restore")
        return
    state = iterm_state() or []
    plan, window_id = plan_restore(panes, state, rules)
    seen = set()
    for step in plan:
        c = step["pane"].get("claude")
        how = "idle pane" if step["use"] else ("new tab" if step["new_tab"] and step["title"] not in seen else "new split")
        seen.add(step["title"])
        what = f"{c.get('title') or c['session_id']}" if c else f"shell {step['pane'].get('cwd')}"
        if c:
            what += "  (re-attach)" if c.get("attach") else "  (resume)"
        print(f"  {step['title']:<16} [{how:<9}] {what}")
    script = build_applescript(plan, window_id, a.delay)
    if a.dry_run:
        if a.verbose:
            print("\n--- AppleScript (dry run) ---\n" + script)
        return
    r = subprocess.run(["osascript", "-"], input=script, text=True, capture_output=True)
    if r.returncode:
        sys.exit("osascript failed: " + r.stderr)
    new_tabs = [tuple(l.split("\t", 1)) for l in r.stdout.strip().splitlines() if "\t" in l]
    set_tab_titles(new_tabs)
    print(f"\nrestored {len(plan)} panes")


def busy_sessions():
    mine = set()
    pid = os.getpid()  # don't wait for the Claude session that's running this command
    for _ in range(12):
        line = sh(["ps", "-o", "ppid=", "-p", str(pid)]).strip()
        if not line:
            break
        pid = int(line)
        mine.add(pid)
    return [c for c in live_claude_sessions(process_table()) if c.get("status") == "busy" and c["pid"] not in mine]


def cmd_prepare(a):
    if a.cancel:
        if os.path.exists(FREEZE_FILE):
            os.remove(FREEZE_FILE)
        print("freeze cancelled; background snapshots resume")
        return
    print("1. Claude sessions still mid-turn")
    deadline = time.time() + a.wait * 60
    while True:
        busy = busy_sessions()
        if not busy or time.time() >= deadline:
            break
        print(f"   waiting for {len(busy)} busy session(s)...", end="\r", flush=True)
        time.sleep(5)
    for c in busy:
        _, path = find_transcript(c["session_id"], c["config_dir"])
        title = transcript_info(path)["title"] if path else None
        print(f"   ⚠ busy: {title or c['session_id']}  ({c['cwd']})")
    if not busy:
        print("   all idle ✓")
    elif not a.wait:
        print("   (they'll resume fine, but their in-flight turn is lost; `prepare --wait 10` waits for them)")

    print("2. iTerm2 layout")
    a.force, a.auto, a.quiet = True, False, True
    cmd_snapshot(a)
    snap = json.load(open(os.path.join(STATE_DIR, "latest.json")))
    n = sum(1 for p in snap["panes"] if p["claude"])
    tabs = len({(p["window"], p["tab"]) for p in snap["panes"]})
    print(f"   saved {len(snap['panes'])} panes in {tabs} tabs, {n} Claude sessions ({snap['source']})")
    json.dump({"boot_time": boot_time(), "until": time.time() + a.freeze * 60, "taken_at": snap["taken_at"]},
              open(FREEZE_FILE, "w"))
    print(f"   frozen for {a.freeze} min, so closing windows/quitting iTerm2 can't overwrite it "
          f"(`claude-restore prepare --cancel` to undo)")

    print("3. Firefox tabs")
    r = subprocess.run([VENV_PY, os.path.join(HERE, "firefox_tabs.py"), "save"], capture_output=True, text=True)
    print("   " + (r.stdout.strip() or r.stderr.strip().splitlines()[-1] if (r.stdout or r.stderr) else "nothing saved"))

    print("\nReady to reboot. Afterwards: open iTerm2, then `claude-restore` (and `claude-restore firefox` if needed).")


def cmd_firefox(a):
    os.execv(VENV_PY, [VENV_PY, os.path.join(HERE, "firefox_tabs.py")] + (a.args or ["list"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--quiet", action="store_true"); s.add_argument("--force", action="store_true")
    s.add_argument("--auto", action="store_true", help="background run: skipped while `prepare` has frozen the snapshot")
    s.set_defaults(fn=cmd_snapshot)
    r = sub.add_parser("recover")
    r.add_argument("--window", type=float, default=40, help="minutes before the last pre-boot transcript write to include (default 40; heartbeat is ~16 min)")
    r.add_argument("--until", help="end of window, ISO local time (default: last transcript write before boot)")
    r.add_argument("-o", "--output")
    r.add_argument("--ids", nargs="+", metavar="SESSION_ID", help="recover these sessions instead of inferring from mtimes")
    r.set_defaults(fn=cmd_recover)
    sh_ = sub.add_parser("show"); sh_.add_argument("file", nargs="?")
    sh_.set_defaults(fn=lambda a: show(load_snapshot(a.file)))
    rs = sub.add_parser("restore")
    rs.add_argument("file", nargs="?", help="snapshot/recovered JSON (default: last snapshot before this boot)")
    rs.add_argument("--dry-run", action="store_true")
    rs.add_argument("--claude-only", action="store_true", help="skip plain shell panes")
    rs.add_argument("--only", nargs="+", metavar="SESSION_ID_PREFIX")
    rs.add_argument("--delay", type=float, default=1.0, help="seconds between panes (default 1)")
    rs.add_argument("-v", "--verbose", action="store_true", help="with --dry-run, print the AppleScript")
    rs.add_argument("--fill", action="store_true", help="ignore the recorded split layout; fill idle panes of same-titled tabs")
    rs.add_argument("--keep-windows", action="store_true", help="don't close idle windows (e.g. the default arrangement) afterwards")
    rs.set_defaults(fn=cmd_restore)
    pr = sub.add_parser("prepare", help="get ready to reboot: check busy sessions, freeze a snapshot, save Firefox tabs")
    pr.add_argument("--wait", type=float, default=0, metavar="MIN", help="wait up to MIN minutes for busy sessions")
    pr.add_argument("--freeze", type=float, default=30, metavar="MIN", help="pause background snapshots (default 30)")
    pr.add_argument("--cancel", action="store_true", help="lift the freeze")
    pr.set_defaults(fn=cmd_prepare)
    ff = sub.add_parser("firefox", help="firefox tabs: save [--previous] | list [file] | restore [file] [--dry-run]")
    ff.add_argument("args", nargs=argparse.REMAINDER)
    ff.set_defaults(fn=cmd_firefox)
    ti = sub.add_parser("titles", help="retry setting titles of tabs created by the last restore")
    ti.set_defaults(fn=cmd_titles)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
