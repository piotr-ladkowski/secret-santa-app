"""Small text helpers shared by the pages and the emails."""

import re

from markupsafe import Markup, escape

_URL = re.compile(r"https?://[^\s<>\"']+")


def linkify(text: str | None) -> Markup:
    """Escape the text and turn http(s) URLs into links (wish lists often hold shop links)."""
    text = text or ""
    parts: list[str] = []
    pos = 0
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}")
        start = match.start()
        parts.append(escape(text[pos:start]))
        parts.append(
            Markup(
                '<a href="{0}" rel="noopener noreferrer nofollow" target="_blank">{0}</a>'
            ).format(url)
        )
        pos = start + len(url)
    parts.append(escape(text[pos:]))
    return Markup("").join(parts)
