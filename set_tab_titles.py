#!/usr/bin/env python3
"""set_tab_titles.py <session-uuid> <title> [...]: set the title of the tab containing each session.
Needs the iTerm2 Python API (Settings > General > Magic > Enable Python API)."""
import sys
import iterm2

pairs = dict(zip(sys.argv[1::2], sys.argv[2::2]))


async def main(connection):
    app = await iterm2.async_get_app(connection)
    for uid, title in pairs.items():
        session = app.get_session_by_id(uid)
        if session and session.tab:
            await session.tab.async_set_title(title)

iterm2.run_until_complete(main)
