"""Guards the SPA index against the r5 blank-page regression.

``hermes_cli/web_server.py`` injects its bootstrap into index.html with

    html.replace("</head>", f"{bootstrap_script}</head>", 1)

— FIRST occurrence only. On 2026-08-15 a comment added to ``web/index.html``
contained a literal closing-head tag in its prose. The injection landed inside
that comment, which swallowed the bootstrap AND every ``<script>`` /
``<link rel=stylesheet>`` tag after it. The browser skipped the whole block as
comment text and issued **zero** asset requests, so the dashboard rendered
blank on every route except the server-rendered login page.

Nothing in the suite caught it: each asset URL still returned 200 when fetched
directly — they had simply stopped being tags. That is what the second test
here checks, and why it strips comments before asserting.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOURCE_INDEX = REPO / "web" / "index.html"
BUILT_INDEX = REPO / "hermes_cli" / "web_dist" / "index.html"

CLOSING_HEAD = "</hea" + "d>"  # split so this file is not its own tripwire


def test_source_index_has_exactly_one_closing_head():
    """No comment or attribute may repeat the tag the injector anchors on."""
    html = SOURCE_INDEX.read_text(encoding="utf-8")
    count = html.count(CLOSING_HEAD)
    assert count == 1, (
        f"web/index.html contains {count} occurrences of the closing head tag. "
        "web_server.py injects at the FIRST one, so a second occurrence "
        "(typically inside a comment) silently swallows the bootstrap and "
        "every tag after it. Rephrase the comment instead."
    )


@pytest.mark.skipif(
    not BUILT_INDEX.exists(),
    reason="frontend not built (hermes_cli/web_dist/index.html absent)",
)
def test_built_index_tags_survive_comment_stripping():
    """The entry script and stylesheet must be real tags, not comment text."""
    html = BUILT_INDEX.read_text(encoding="utf-8")
    assert html.count("<!--") == html.count("-->"), "unbalanced HTML comment"

    stripped = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    assert '<script type="module"' in stripped, (
        "the entry module script is inside an HTML comment — the SPA will "
        "load nothing and every route renders blank"
    )
    assert 'rel="stylesheet"' in stripped, (
        "the stylesheet link is inside an HTML comment"
    )
    assert html.count(CLOSING_HEAD) == 1
