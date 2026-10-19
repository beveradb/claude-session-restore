# claude-session-restore

Records which iTerm2 tab and split every Claude Code session lives in, so a restart (planned,
or a dead battery) doesn't lose track of what was open, and puts them back in the same tabs.

## Before a planned reboot

```sh
claude-restore prepare            # or: prepare --wait 10  (wait up to 10 min for busy sessions)
```

1. Lists Claude sessions still mid-turn. They resume fine afterwards, but the in-flight turn is lost.
2. Takes a final iTerm2 snapshot and **freezes** it for 30 min (`--freeze MIN`). Background
   snapshots pause, so windows closing during shutdown can't overwrite it. The freeze lapses
   on its own if you don't reboot. `prepare --cancel` lifts it early.
3. Saves every Firefox window and tab (see below).

Then restart from the Apple menu. You don't need to quit iTerm2 or touch tmux. Quitting
iTerm2 alone doesn't stop claude-auto-retry's tmux sessions; a reboot does, and that's harmless
because transcripts are written as you go.

## Firefox tabs

```sh
claude-restore firefox save [--previous]     # --previous: the session Firefox didn't restore (after a crash)
claude-restore firefox list [file]
claude-restore firefox restore [file] [--dry-run] [--window N]
```

Reads Firefox's own session files (`recovery.jsonlz4` / `sessionstore.jsonlz4` / `previous.jsonlz4`,
mozLz4-compressed) and writes `~/.local/state/claude-session-restore/firefox/<ts>.json` plus a
clickable `.html` list. `restore` reopens each window with its tabs (URLs and window grouping
only; not pinned state, containers or tab groups). By default it uses the newest file from
before the current boot.

## After a restart

1. Open iTerm2. It's simplest if it opens a single plain window: turn off
   Settings › General › Startup › "Open default window arrangement" (restore would close an
   all-idle startup window anyway).
2. `claude-restore --dry-run` lists every window, tab and pane with its size and what will run there.
3. `claude-restore` (**don't type while it runs**).

Restore uses the **newest snapshot taken before the current boot**, so a snapshot taken after
you log back in can't shadow it. With a layout snapshot (the normal case) it:

1. Recreates every window at its saved position and size, with tabs in order and titled.
2. Rebuilds each tab's split tree divider by divider, re-applies each pane's font
   (⌘+/⌘− changes are per pane), then sets each pane to its saved columns × rows.
   Expect within about a column of the original.
3. Sends each pane its command: `cd <dir> && <load direnv> && CLAUDE_CONFIG_DIR=<cfg> claude --resume <id>`,
   or `tmux attach` if the session is still running detached (claude-auto-retry keeps it alive
   when you close a window), or just `cd <dir>` for plain shells.
4. Closes windows that hold nothing but idle shells (e.g. the default arrangement opened at
   login), including the one you ran it from, if that pane is also idle. `--keep-windows` disables this.

Sessions already visible in another pane are left alone, and their slot becomes a plain shell.

Other modes: `--fill` ignores the saved layout and drops sessions into idle panes of same-titled
tabs, splitting or creating tabs as needed. It's also used automatically for `--only`,
`--claude-only` and recovered files, which have no layout.

## Other commands

```sh
claude-restore show [file]     # what a snapshot contains, grouped by tab
claude-restore snapshot        # take one now (launchd does it every 60s)
claude-restore titles          # retry titling new tabs (after enabling the Python API)
claude-restore prepare         # before a planned reboot (see above)
claude-restore history [WEEK]  # weekly archive: list weeks, or show one (e.g. 2026-W42)
claude-restore firefox ...     # Firefox tabs (see above)
claude-restore recover [--ids <id>...] [--window 40] [--until ISO]
./scan_sessions.py --days 8    # every recent interactive session: title, last prompt, branch
```

`recover` is the fallback when no snapshot exists. Live Claude processes touch their transcript
`.jsonl` every ~16 minutes even when idle, so the transcripts touched just before shutdown
are the sessions that were open. It writes a JSON file with a `tab_title` for each session
(from `tabs.json`); edit it, or set `"enabled": false`, then `claude-restore <file>`.

## tabs.json

Used only for panes with no recorded tab title (recovered sessions, or tabs you never named).
The first matching rule wins. A rule can match `cwd_prefix` (plain string prefix, checked
against the launch dir *and* the transcript's most-used working dir), `config_dir`, and/or
`session` (id prefix).

## How it works

- **Snapshot** (`uk.beveridge.claude-session-snapshot` LaunchAgent, every 60s): `iterm_layout.py capture`
  (iTerm2 Python API) records window frames, tab titles, split trees with orientation, and each
  pane's columns × rows, font, tty, cwd and profile. If the API is unavailable, AppleScript
  (tab titles, pane order and sizes, but no tree) and then env vars are used.
  Each config dir's `sessions/<pid>.json` registry gives the live Claude sessions. A session is
  matched to a pane by tty: through `tmux list-clients` when wrapped by claude-auto-retry,
  otherwise the process's own tty. Argv is recorded, so flags like `--dangerously-skip-permissions` are kept.
  A history file is written only when something changes. History and saved Firefox tabs are pruned on each snapshot: anything older than 14 days or beyond 1,000 files goes, but the newest 50 are always kept, so an unchanged layout never loses its last snapshot.
- **Weekly archive (kept forever)**: once an ISO week ends, its last snapshot is copied to
  `snapshots/weekly/<YYYY-Www>.json`, with each session's title, working dir and last prompt
  saved into it, because Claude Code eventually deletes old transcripts (`cleanupPeriodDays`).
  The last saved Firefox tabs of the week go to `firefox/weekly/` (with the `.html`). About
  12 KB per week. Browse with `claude-restore history`.
  State lives in `~/.local/state/claude-session-restore/`, logs in
  `~/Library/Logs/claude-session-snapshot.log`.
- **Fallback when AppleScript fails:** positions come from `ITERM_SESSION_ID` in each claude
  process's env, and from `pane-hook.zsh` (sourced in `~/.zshrc`; idle shells report pane id + cwd).
- **Tab titles:** AppleScript can read tab titles but not set them. New tabs are titled with
  `set_tab_titles.py` through the iTerm2 Python API (`.venv`; enable it under
  Settings › General › Magic). `~/.tmux.conf` passes the title Claude sets on each pane
  (`✳ <session title>`) through tmux, but it doesn't override a tab's own title.
- **Geometry**: siblings are created by splitting the previous sibling in the parent's
  orientation before recursing, so every split lands in the right splitter. Sizes are applied
  via `preferred_size` + `Tab.async_update_layout()`. Only `--fill` mode approximates
  (alternating splits).

## Install

macOS + iTerm2 + Claude Code. Python API recommended (Settings › General › Magic › Enable Python API).

```sh
git clone https://github.com/beveradb/claude-session-restore && cd claude-session-restore
./install.sh   # venv + iterm2/lz4 pkgs, ~/.local/bin/claude-restore, LaunchAgent, zsh pane hook
```

Every `~/.claude*` dir that contains `projects/` counts as a Claude Code config dir, so
per-project `CLAUDE_CONFIG_DIR`s like `~/.claude-work` are found automatically.

Personal, git-ignored config:

- `tabs.json`: tab rules for recovered sessions (created from `tabs.example.json`).
- `local.json`: optional, e.g. `{"skip_permissions_alias": "cld", "default_flags": ["--dangerously-skip-permissions"]}`.
  With an alias set, a session started with `--dangerously-skip-permissions` is resumed by
  typing the alias (`cld --resume <id>`). `default_flags` are the flags for recovered sessions
  with no recorded argv.

## Gotcha: shared tmux server and CLAUDE_CONFIG_DIR

claude-auto-retry runs every claude inside one tmux server. A tmux session started from a shell
that doesn't set `CLAUDE_CONFIG_DIR` (the `~/.claude` default) inherits the *server's* value:
whatever the shell that first started the server had, e.g. another project's config dir. Claude
then can't find the transcript ("No conversation found with session ID"), and new sessions land
in the wrong config. `install.sh` adds this to `~/.tmux.conf`:

```
set -ga update-environment CLAUDE_CONFIG_DIR
```

With it, tmux copies the variable from the launching shell, or removes it when unset. Restore
also clears it from the server before resuming a `~/.claude` session.

## License

MIT
