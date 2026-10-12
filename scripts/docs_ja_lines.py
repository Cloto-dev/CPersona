#!/usr/bin/env python3
"""Join the lines of a Japanese paragraph where the source wraps between two Japanese characters.

The Japanese pages are wrapped at about eighty columns like the English ones,
and a line break inside a paragraph is white space. Between two English words
that is the space they need. Between two Japanese characters it is a space that
should not be there: Chrome and Safari render it, so the published pages read
"Claude を はじめとする" where the source says "Claude を" / "はじめとする"
(measured 2026-10-12: 4,638 such breaks in the 42 Japanese sources, 51 on the
Japanese Getting Started page alone). Firefox applies the CSS Text rule that
removes the break between wide characters, so it already showed the text right.

The sources stay wrapped -- rewrapping them would put every Japanese line in
every translation diff. The join happens here, as a build hook like
docs_version.py, while the page is still markdown, and only on the Japanese
copies. A break is joined when the line before it ends in a Japanese character
and the line after it, past its indentation and any blockquote marker it
shares with the line before, starts with one. Emphasis and code markers next
to the character (`**`, a backtick, an opening `[`) do not stop the join; an
underscore does, because Python-Markdown does not close `_` emphasis before a
letter, and a Japanese character is a letter to it.
Fenced code blocks, headings and lines ending in a hard break (two spaces) are
left as they are. A line that starts with a Japanese character cannot be a
list item, heading, table row or fence, so the join never merges two blocks.
"""

from __future__ import annotations

import re

# Japanese punctuation, kana, the CJK unified ideographs and the full-width forms.
JA = "\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\uff00-\uffef"
ENDS_JA = re.compile(rf"[{JA}][*`]*$")
STARTS_JA = re.compile(rf"^[*`\[]*[{JA}]")
QUOTE = re.compile(r"^((?:[ \t]*>)*)[ \t]*")
FENCE = re.compile(r"^[ \t]*(```|~~~)")


def join_lines(markdown: str) -> str:
    out: list[str] = []
    fence: str | None = None
    for line in markdown.split("\n"):
        m = FENCE.match(line)
        if fence:
            out.append(line)
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            continue
        if out and _joinable(out[-1], line):
            out[-1] = out[-1].rstrip() + QUOTE.sub("", line, count=1)
            continue
        out.append(line)
    return "\n".join(out)


def _joinable(prev: str, line: str) -> bool:
    if prev.endswith("  ") or prev.lstrip().startswith("#") or FENCE.match(prev):
        return False
    if not ENDS_JA.search(prev.rstrip()):
        return False
    prev_quote = QUOTE.match(prev).group(1).replace(" ", "").replace("\t", "")
    quote = QUOTE.match(line)
    if quote.group(1).replace(" ", "").replace("\t", "") != prev_quote:
        return False
    return bool(STARTS_JA.match(line[quote.end():]))


def on_page_markdown(markdown: str, page, **_kwargs) -> str:
    if page.file.src_uri.endswith(".ja.md"):
        return join_lines(markdown)
    return markdown
