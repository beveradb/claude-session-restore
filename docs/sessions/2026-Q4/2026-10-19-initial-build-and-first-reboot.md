# Initial build + first real reboot — 2026-10-19

**Project:** claude-session-restore   **Branch/commit:** main @ f8f01dc   **Status:** done (in daily use)

## Summary
The Mac died from a flat battery overnight, and every iTerm2 tab and split, with its Claude Code
session, was lost. iTerm2's own restorable state was wiped when it relaunched, and there were no
Time Machine snapshots. This session built the tool from nothing:
1. **Forensic recovery** worked out which sessions had been open.
2. **Snapshot** records every iTerm2 window, tab and split, plus the Claude session in each pane,
   every 60s.
3. **Restore** rebuilds the exact layout and resumes or re-attaches each session.
4. **`prepare`** and Firefox tab save/restore cover planned reboots.
5. A **weekly archive** keeps history.

Tested through a real macOS-update reboot. Everything came back except the sessions on the
default `~/.claude` config; fixed (see gotchas).

## What changed
- New public repo https://github.com/beveradb/claude-session-restore (MIT).
  - `claude_sessions.py`: snapshot, recover, show, restore (exact and `--fill` modes), titles,
    prepare, history, firefox.
  - `iterm_layout.py`: Python API capture and build.
  - `firefox_tabs.py`: mozLz4 session reader.
  - `scan_sessions.py`, `pane-hook.zsh`, `install.sh`, `launchagent.plist.template`.
- Installed on the Mac:
  - LaunchAgent `uk.beveridge.claude-session-snapshot`: `snapshot --quiet --auto` every 60s.
  - `~/.local/bin/claude-restore` symlink.
  - Pane hook sourced from `~/.zshrc`.
  - `.venv` with `iterm2` and `lz4`.
- State lives in `~/.local/state/claude-session-restore/`: `latest.json`, `snapshots/` (+ `weekly/`),
  `firefox/` (+ `weekly/`), `recovered-*.json`, `frozen.json`.
- iTerm2 settings:
  - Python API enabled (it only actually started after re-ticking it in the UI).
  - Default profile font set to Monaco 10.
  - `OpenArrangementAtStartup` set to false, so startup opens a plain window; restore closes
    all-idle windows.
- `~/.tmux.conf`:
  - `set-titles on` / `set-titles-string "#T"`
  - `set -ga update-environment CLAUDE_CONFIG_DIR`

## Decisions & rationale
- **Exact layout via the iTerm2 Python API.** It gives the split tree plus `preferred_size` and
  `Tab.async_update_layout()`. Saved arrangements were the alternative, but they offer no way to
  map a recreated pane to its session.
- **Siblings first, then recurse.** All siblings of a splitter are created by splitting the
  previous sibling in the parent's orientation, before recursing into children; otherwise splits
  land in the wrong splitter. Pane sizes come back within about one cell; macOS nudges new window
  frames a few px.
- **Per-pane fonts are captured and re-applied.** ⌘−/⌘+ changes the font per session, not in the
  profile, so the cell grid differs between tabs.
- **Claude sessions are matched to panes by tty.**
  - With claude-auto-retry: through `tmux list-clients`.
  - Otherwise: the process's own tty.
  - Not `ITERM_SESSION_ID` env: a tmux session re-attached in a new pane keeps the env of the pane
    it was launched in.
- **Restore re-attaches still-running detached tmux sessions instead of resuming them twice.**
  Closing an iTerm2 window doesn't kill claude-auto-retry's tmux sessions.
- **Default snapshot = the newest one taken before the current boot.** `prepare` freezes
  background snapshots for 30 min, so windows closing during shutdown can't overwrite the good
  one.
- **Pruning:** history is deleted after 14 days or beyond 1,000 files, but the newest 50 are
  always kept (an age-only prune could delete the only snapshot). One snapshot per ISO week is kept
  forever, with session titles and last prompts saved into it.
- **Personal config stays git-ignored:** `tabs.json`, `local.json` (`{"skip_permissions_alias": "cld"}`).
  Config dirs are found automatically as any `~/.claude*` dir containing `projects/`.

## Learnings / gotchas
- **Transcript mtime heartbeat.** Live Claude Code processes touch their transcript `.jsonl`
  about every 16 min even when idle; this is the basis of `recover`. Resuming a session moves its
  mtime past boot time, so use `recover --ids` afterwards.
- **`<config>/sessions/<pid>.json`** is a live registry (`sessionId`, `cwd`, `status`
  busy/idle, `tmux`, `kind`). Stale entries survive a crash until a session starts in that config.
- **macOS hides login-shell environments from `ps eww`.** Hence the zsh pane hook as a fallback.
  AppleScript to iTerm2 works from launchd without prompts.
- **AppleScript can read tab titles (`variable named "tab.titleOverride"`) but can't set them.**
  Only the Python API can (`tab.async_set_title`). Inside `tell application "iTerm2"`, `tab` means
  the class, not a tab character; use `character id 9`.
- **The shared tmux server leaks env.** claude-auto-retry's server keeps the startup env of
  whichever shell started it, so a session launched with `CLAUDE_CONFIG_DIR` unset inherits
  another project's value. Result: "No conversation found with session ID", and new sessions land
  in the wrong config. Fixed with tmux `update-environment`, and restore also runs
  `tmux set-environment -g -u`. Setting `CLAUDE_CONFIG_DIR=~/.claude` explicitly is NOT a fix,
  because Claude then looks for `~/.claude/.claude.json`.
- **iTerm2 Python API toggling.** `defaults write EnableAPIServer` makes the checkbox appear
  ticked but doesn't start the server until it's toggled in the UI or iTerm2 relaunches.
- **Firefox:** `recovery.jsonlz4`, `previous.jsonlz4` and `lastSessionState` keep the session it
  didn't restore after a crash. Format: `b"mozLz40\0"` + lz4 block.

## Open threads & next steps
- Possibly report the claude-auto-retry tmux env leak upstream (cheapestinference/claude-auto-retry).
- Ideas not built:
  - iTerm2 AutoLaunch script offering restore at login.
  - Per-pane title provider (project names on tabs, session titles on splits).
  - Weekly prompt to close `/wrap`ped or idle tabs.
  - fzf picker over snapshot history.
- `--fill` mode and `recover` lay out new splits approximately; only exact mode matches
  geometry.
- The first real weekly archive (2026-W43) is written after Sunday 2026-10-25; check that
  `claude-restore history` shows it.

## Related docs
- README.md (usage, internals, tmux gotcha)
