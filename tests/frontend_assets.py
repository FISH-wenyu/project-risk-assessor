"""Loaders for the extracted frontend assets.

The page used to be one inline `INDEX_HTML` string in `app/main.py`, and the
frontend tests asserted against it. Now that the markup, CSS and script live in
`app/static/`, tests read them from disk instead.

`page_source()` returns markup and script concatenated, which is what the
old assertions were really checking: "does the delivered page contain this".
Tests that care specifically about one layer should use the narrower loaders.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"


@lru_cache(maxsize=None)
def index_html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def app_css() -> str:
    return (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def app_js() -> str:
    return (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")


def script_names() -> tuple[str, ...]:
    """Script files in the order `index.html` loads them.

    Read from the markup rather than listed here, so a script added to the page
    without a matching test loader cannot go unnoticed, and so splitting one
    module into several does not silently drop the new files from every
    assertion that greps the delivered script.
    """
    return tuple(re.findall(r'<script src="/static/js/([^"?]+)"', index_html()))


@lru_cache(maxsize=None)
def contracts_js() -> str:
    """Every contract-module script, concatenated in load order.

    `contracts.js` was one file and is now several. Tests assert on behaviour,
    not on which file holds it, so they read the module as a whole.
    """
    parts = [
        (STATIC_DIR / "js" / name).read_text(encoding="utf-8")
        for name in script_names()
        if name.startswith("contract")
    ]
    return "\n".join(parts)


@lru_cache(maxsize=None)
def page_source() -> str:
    """Everything the browser receives: markup, styles and script.

    This is the direct replacement for the old single `INDEX_HTML` string, so
    assertions that just ask "is this in the delivered page" keep working
    regardless of which file the content now lives in.
    """
    return "\n".join((index_html(), app_css(), app_js()))


def referenced_element_ids() -> dict[str, set[str]]:
    """`getElementById` arguments per script file.

    Used to catch the failure mode that the inline-string era could not have:
    markup and script now live in different files, so an element can be deleted
    from the page while the code that reaches for it stays behind.
    """
    found: dict[str, set[str]] = {}
    for name in script_names():
        source = (STATIC_DIR / "js" / name).read_text(encoding="utf-8")
        found[name] = set(re.findall(r'getElementById\("([^"]+)"\)', source))
    return found


def declared_element_ids() -> set[str]:
    return set(re.findall(r'id="([^"]+)"', index_html()))
