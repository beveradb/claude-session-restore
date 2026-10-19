#!/usr/bin/env python3
"""Capture / rebuild iTerm2 windows, tabs and exact split layouts via the iTerm2 Python API.

  iterm_layout.py capture        -> JSON on stdout: windows (frame), tabs (title), split trees,
                                    panes (session id, cols x rows, tty, cwd, profile)
  iterm_layout.py build < plan   -> recreate the windows in `plan` (same JSON shape, leaves may
                                    carry "command" and "name"), then close `close_window_ids`

Runs under .venv (needs the `iterm2` package and Settings > General > Magic > Enable Python API).
"""
import asyncio
import json
import sys

import iterm2
from iterm2.util import Frame, Point, Size


async def capture_node(node):
    if isinstance(node, iterm2.Splitter):
        return {"vertical": node.vertical, "children": [await capture_node(c) for c in node.children]}
    s = node
    var = s.async_get_variable
    font = None
    try:  # per-session font (⌘+/⌘− changes it without touching the profile)
        font = (await s.async_get_profile()).normal_font
    except Exception:
        pass
    return {"session_id": s.session_id, "cols": s.grid_size.width, "rows": s.grid_size.height,
            "tty": await var("tty"), "path": await var("path"), "profile": await var("profileName"),
            "font": font, "auto_name": await var("autoName")}


async def capture(connection):
    app = await iterm2.async_get_app(connection)
    windows = []
    for w in app.windows:
        f = await w.async_get_frame()
        tabs = []
        for t in w.tabs:
            tabs.append({"tab_id": t.tab_id, "title": await t.async_get_variable("titleOverride") or None,
                         "root": await capture_node(t.root)})
        windows.append({"window_id": w.window_id, "frame": [f.origin.x, f.origin.y, f.size.width, f.size.height],
                        "tabs": tabs})
    print(json.dumps({"windows": windows}))


def first_leaf(node):
    while "children" in node:
        node = node["children"][0]
    return node


def leaves(node):
    if "children" in node:
        return [l for c in node["children"] for l in leaves(c)]
    return [node]


async def split(session, vertical, profile):
    try:
        return await session.async_split_pane(vertical=vertical, profile=profile)
    except Exception:
        return await session.async_split_pane(vertical=vertical)  # profile was deleted


async def build_node(node, session, made):
    """Give `node` the area currently occupied by `session`. All siblings are created first (by
    splitting the previous sibling with the parent's orientation), then each child recurses, so
    every split lands in the right splitter."""
    if "children" not in node:
        made.append((node, session))
        return
    sessions = [session]
    for child in node["children"][1:]:
        sessions.append(await split(sessions[-1], node["vertical"], first_leaf(child).get("profile")))
    for child, s in zip(node["children"], sessions):
        await build_node(child, s, made)


async def build(connection, plan):
    app = await iterm2.async_get_app(connection)
    made = []
    for wplan in plan["windows"]:
        prof = first_leaf(wplan["tabs"][0]["root"]).get("profile")
        try:
            window = await iterm2.Window.async_create(connection, profile=prof)
        except Exception:
            window = await iterm2.Window.async_create(connection)
        if wplan.get("frame"):
            x, y, w, h = wplan["frame"]
            await window.async_set_frame(Frame(Point(x, y), Size(w, h)))
        sized = []
        for i, tplan in enumerate(wplan["tabs"]):
            if i == 0:
                tab = window.current_tab
            else:
                prof = first_leaf(tplan["root"]).get("profile")
                try:
                    tab = await window.async_create_tab(profile=prof)
                except Exception:
                    tab = await window.async_create_tab()
            tab_made = []
            await build_node(tplan["root"], tab.current_session, tab_made)
            for leaf, s in tab_made:
                if leaf.get("font"):
                    lw = iterm2.LocalWriteOnlyProfile()
                    lw.set_normal_font(leaf["font"])
                    await s.async_set_profile_properties(lw)
            if tplan.get("title"):
                await tab.async_set_title(tplan["title"])
            made += tab_made
            sized.append((tab, tab_made))
        # Second pass once all tabs exist: the window may have been resized/constrained while
        # tabs were added, so re-apply the frame and then every tab's pane sizes.
        if wplan.get("frame"):
            await window.async_set_frame(Frame(Point(x, y), Size(w, h)))
            await asyncio.sleep(0.3)
        for tab, tab_made in sized:
            for leaf, s in tab_made:
                if leaf.get("cols") and leaf.get("rows"):
                    s.preferred_size = Size(leaf["cols"], leaf["rows"])
            if len(tab_made) > 1:
                await tab.async_update_layout()
    for leaf, s in made:
        if leaf.get("name"):
            await s.async_set_name(leaf["name"])
    for leaf, s in made:  # commands last, staggered, so a dozen claude launches don't stampede
        if leaf.get("command"):
            await s.async_send_text(leaf["command"] + "\n")
            await asyncio.sleep(plan.get("delay", 1.0))
    print(json.dumps({"created": [[leaf.get("key"), s.session_id] for leaf, s in made]}), flush=True)
    app = await iterm2.async_get_app(connection)
    for wid in plan.get("close_window_ids", []):
        w = app.get_window_by_id(wid)
        if w:
            await w.async_close(force=True)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "capture":
        iterm2.run_until_complete(capture)
    elif cmd == "build":
        plan = json.load(sys.stdin)
        iterm2.run_until_complete(lambda c: build(c, plan))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
