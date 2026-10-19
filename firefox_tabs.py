#!/usr/bin/env python3
"""Save / restore Firefox windows+tabs independently of Firefox's own session restore.

  firefox_tabs.py save [--previous]     write ~/.local/state/claude-session-restore/firefox/<ts>.{json,html}
                                        (--previous: the last session Firefox did NOT restore, e.g. after a crash)
  firefox_tabs.py list [file]           show windows/tabs of a saved file (default: newest)
  firefox_tabs.py restore [file] [--dry-run] [--window N]
                                        reopen each saved window with its tabs

Reads Firefox's session files (mozLz4: b"mozLz40\\0" + lz4 block). Runs under .venv (needs `lz4`).
Restores URLs, window grouping and order; not pinned state, containers, tab groups or history.
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import subprocess
import sys
import time

import lz4.block

HOME = os.path.expanduser("~")
PROFILES = os.path.join(HOME, "Library", "Application Support", "Firefox", "Profiles")
OUT_DIR = os.path.join(HOME, ".local", "state", "claude-session-restore", "firefox")
FIREFOX = "/Applications/Firefox.app/Contents/MacOS/firefox"
SKIP_PREFIXES = ("about:newtab", "about:blank", "about:home", "about:privatebrowsing")


def load_mozlz4(path):
    with open(path, "rb") as f:
        b = f.read()
    if b[:8] != b"mozLz40\0":
        raise ValueError(f"{path}: not a mozLz4 file")
    return json.loads(lz4.block.decompress(b[8:]))


def firefox_running():
    return subprocess.run(["pgrep", "-f", "Firefox.app/Contents/MacOS/firefox"], capture_output=True).returncode == 0


def session_file(profile):
    """The freshest copy of the live session: recovery.jsonlz4 while Firefox runs (written every
    ~15s), sessionstore.jsonlz4 after a clean quit."""
    cands = [os.path.join(profile, "sessionstore-backups", "recovery.jsonlz4"),
             os.path.join(profile, "sessionstore.jsonlz4"),
             os.path.join(profile, "sessionstore-backups", "recovery.baklz4")]
    cands = [c for c in cands if os.path.exists(c)]
    return max(cands, key=os.path.getmtime) if cands else None


def extract(state):
    windows = []
    for w in state.get("windows", []):
        groups = {g.get("id"): g.get("name") for g in w.get("groups", [])}
        tabs = []
        for t in w.get("tabs", []):
            entries = t.get("entries") or []
            if not entries:
                continue
            e = entries[min(max(t.get("index", len(entries)), 1), len(entries)) - 1]
            url = e.get("url") or ""
            if not url or url.startswith(SKIP_PREFIXES):
                continue
            tabs.append({"url": url, "title": e.get("title") or url, "pinned": bool(t.get("pinned")),
                         "group": groups.get(t.get("groupId")), "container": t.get("userContextId") or None})
        if tabs:
            windows.append({"selected_tab": w.get("selected"), "tabs": tabs})
    return windows


def cmd_save(a):
    os.makedirs(OUT_DIR, exist_ok=True)
    profiles = []
    for prof in sorted(glob.glob(os.path.join(PROFILES, "*"))):
        f = session_file(prof)
        if not f:
            continue
        state = load_mozlz4(f)
        if a.previous:
            state = state.get("lastSessionState") or {}
            if not state.get("windows"):
                prev = os.path.join(prof, "sessionstore-backups", "previous.jsonlz4")
                state = load_mozlz4(prev) if os.path.exists(prev) else {}
        windows = extract(state)
        if windows:
            profiles.append({"profile": os.path.basename(prof), "source": f, "windows": windows})
    if not profiles:
        print("firefox: no windows with tabs found")
        return
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ("-previous" if a.previous else "")
    out = {"taken_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "previous_session": a.previous, "profiles": profiles}
    path = os.path.join(OUT_DIR, stamp + ".json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    write_html(out, path[:-5] + ".html")
    n_w = sum(len(p["windows"]) for p in profiles)
    n_t = sum(len(w["tabs"]) for p in profiles for w in p["windows"])
    print(f"firefox: saved {n_t} tabs in {n_w} window(s) -> {path} (+ .html)")


def write_html(out, path):
    parts = ["<!doctype html><meta charset=utf-8><title>Firefox tabs " + html.escape(out["taken_at"]) + "</title>",
             "<style>body{font:14px system-ui;max-width:60em;margin:2em auto;padding:0 1em}li{margin:.2em 0}</style>",
             f"<h1>Firefox tabs saved {html.escape(out['taken_at'])}</h1>"]
    for p in out["profiles"]:
        for i, w in enumerate(p["windows"], 1):
            parts.append(f"<h2>{html.escape(p['profile'])} — window {i} ({len(w['tabs'])} tabs)</h2><ol>")
            for t in w["tabs"]:
                extra = " 📌" if t["pinned"] else ""
                extra += f" <small>[{html.escape(t['group'])}]</small>" if t.get("group") else ""
                parts.append(f'<li><a href="{html.escape(t["url"])}">{html.escape(t["title"])}</a>{extra}</li>')
            parts.append("</ol>")
    with open(path, "w") as f:
        f.write("\n".join(parts))


def newest(before_boot=False):
    files = sorted(glob.glob(os.path.join(OUT_DIR, "*.json")), key=os.path.getmtime)
    if before_boot:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True).stdout
        bt = int(out.split("sec = ")[1].split(",")[0])
        files = [f for f in files if os.path.getmtime(f) < bt] or files
    if not files:
        sys.exit("firefox: no saved tabs yet (run: claude-restore firefox save)")
    return files[-1]


def cmd_list(a):
    path = a.file or newest(before_boot=True)
    d = json.load(open(path))
    print(f"{path}  (saved {d['taken_at']})")
    for p in d["profiles"]:
        for i, w in enumerate(p["windows"], 1):
            print(f"  [{p['profile']}] window {i}: {len(w['tabs'])} tabs")
            for t in w["tabs"]:
                print(f"     {'📌 ' if t['pinned'] else ''}{t['title'][:90]}")


def cmd_restore(a):
    path = a.file or newest(before_boot=True)
    d = json.load(open(path))
    windows = [w for p in d["profiles"] for w in p["windows"]]
    if a.window:
        windows = [windows[a.window - 1]]
    n = sum(len(w["tabs"]) for w in windows)
    print(f"{path}: reopening {n} tabs in {len(windows)} window(s)")
    if a.dry_run:
        for i, w in enumerate(windows, 1):
            print(f"  window {i}: " + ", ".join(t["title"][:40] for t in w["tabs"]))
        return
    if not firefox_running():
        subprocess.Popen(["open", "-a", "Firefox"])
        time.sleep(4)
    for w in windows:
        urls = [t["url"] for t in w["tabs"]]
        subprocess.run([FIREFOX, "--new-window", urls[0]])
        time.sleep(1.5)  # let the window become the target for --new-tab
        for u in urls[1:]:
            subprocess.run([FIREFOX, "--new-tab", u])
            time.sleep(0.2)
    print("done (pinned tabs, containers and tab groups are not recreated; see the .html for details)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save"); s.add_argument("--previous", action="store_true"); s.set_defaults(fn=cmd_save)
    l = sub.add_parser("list"); l.add_argument("file", nargs="?"); l.set_defaults(fn=cmd_list)
    r = sub.add_parser("restore"); r.add_argument("file", nargs="?"); r.add_argument("--dry-run", action="store_true")
    r.add_argument("--window", type=int, help="only this window (1-based)"); r.set_defaults(fn=cmd_restore)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
