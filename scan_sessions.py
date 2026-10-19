#!/usr/bin/env python3
"""Scan Claude Code transcripts across all config dirs and list recently-active sessions."""
import argparse, json, os, glob, datetime as dt

CONFIG_DIRS = sorted(d for d in glob.glob(os.path.expanduser("~/.claude*")) if os.path.isdir(os.path.join(d, "projects")))

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""

def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def scan_file(path, cfg):
    info = dict(config=cfg, file=path, sessionId=os.path.basename(path)[:-6], cwd=None, branch=None,
                entrypoint=None, first=None, last=None, prompts=0, title=None, ai_title=None,
                last_prompt=None, last_cmd=None, last_assistant=None, bridge=False, first_prompt=None)
    with open(path, errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "custom-title":
                info["title"] = o.get("customTitle") or info["title"]
            elif t == "ai-title":
                info["ai_title"] = o.get("aiTitle") or info["ai_title"]
            elif t == "summary" and not info["ai_title"]:
                info["ai_title"] = o.get("summary")
            elif t == "bridge-session":
                info["bridge"] = True
            if t not in ("user", "assistant") or o.get("isSidechain"):
                continue
            ts = parse_ts(o.get("timestamp", ""))
            if ts:
                info["first"] = info["first"] or ts
                info["last"] = ts
            info["cwd"] = o.get("cwd") or info["cwd"]
            info["branch"] = o.get("gitBranch") or info["branch"]
            info["entrypoint"] = o.get("entrypoint") or info["entrypoint"]
            msg = o.get("message") or {}
            txt = text_of(msg.get("content")).strip()
            if t == "user" and txt and not o.get("isMeta"):
                if "<command-name>" in txt:
                    cmd = txt.split("<command-name>")[1].split("</command-name>")[0]
                    info["last_cmd"] = cmd
                    args = txt.split("<command-args>")[1].split("</command-args>")[0] if "<command-args>" in txt else ""
                    info["last_prompt"] = f"{cmd} {args}".strip()
                    info["prompts"] += 1
                elif not txt.startswith(("<local-command", "<system-reminder", "<task-notification", "Caveat:", "[Request interrupted")):
                    info["last_prompt"] = txt
                    info["last_cmd"] = None
                    info["first_prompt"] = info["first_prompt"] or txt
                    info["prompts"] += 1
            elif t == "assistant" and txt:
                info["last_assistant"] = txt
    return info

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=8)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="include headless/sdk sessions and empty ones")
    a = ap.parse_args()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)
    out = []
    for cfg in CONFIG_DIRS:
        for path in glob.glob(os.path.join(cfg, "projects", "*", "*.jsonl")):
            if os.path.getmtime(path) < cutoff.timestamp() or os.path.getsize(path) == 0:
                continue
            i = scan_file(path, cfg)
            if not i["last"] or i["last"] < cutoff:
                continue
            if not a.all and (i["prompts"] == 0 or (i["entrypoint"] or "cli") not in ("cli", "claude-desktop", "remote")):
                continue
            out.append(i)
    out.sort(key=lambda i: i["last"], reverse=True)
    if a.json:
        print(json.dumps(out, default=str, indent=1))
        return
    for i in out:
        loc = i["last"].astimezone().strftime("%a %d %H:%M")
        print(f"{loc}  [{os.path.basename(i['config'])}] {i['cwd']} ({i['branch']})  prompts={i['prompts']} ep={i['entrypoint']}")
        print(f"     id={i['sessionId']}  title={i['title'] or i['ai_title']}")
        print(f"     last> {(i['last_prompt'] or '')[:160]!r}")

if __name__ == "__main__":
    main()
