"""Bounded backend rendering for Stage 4 canonical task bodies."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from dish_service.frontend_contract import RENDERER_CONTRACT_VERSION

_LINK_RE = re.compile(r"\[([^\]\n]{1,500})\]\(([^)\s]{1,2048})\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$")


class DetailCapacityExceeded(ValueError):
    """Configured detail/render bounds would be exceeded."""


class RenderRejected(ValueError):
    """Bounded source could not be represented by the pinned renderer."""


@dataclass(frozen=True, slots=True)
class RenderConfig:
    max_body_chars: int = 100_000
    max_rendered_chars: int = 300_000
    max_lines: int = 10_000

    def __post_init__(self) -> None:
        if min(self.max_body_chars, self.max_rendered_chars, self.max_lines) <= 0:
            raise ValueError("renderer bounds must be positive")


def render_body(body: str, *, config: RenderConfig) -> dict[str, str]:
    """Render a small safe Markdown subset, escaping everything else as text."""

    if not isinstance(body, str) or len(body) > config.max_body_chars:
        raise DetailCapacityExceeded("canonical body exceeds the configured bound")
    lines = body.splitlines()
    if len(lines) > config.max_lines:
        raise DetailCapacityExceeded("canonical body exceeds the configured line bound")

    output: list[str] = []
    output_length = 0
    paragraph: list[str] = []
    list_kind: str | None = None
    code: list[str] | None = None

    def emit(value: str) -> None:
        nonlocal output_length
        output_length += len(value)
        if output_length > config.max_rendered_chars:
            raise DetailCapacityExceeded("rendered body exceeds the configured bound")
        output.append(value)

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            emit(f"<p>{'<br>'.join(_inline(item) for item in paragraph)}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            emit(f"</{list_kind}>")
            list_kind = None

    for line in lines:
        if code is not None:
            if line.startswith("```"):
                emit(f"<pre><code>{html.escape(chr(10).join(code), quote=False)}</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.startswith("```"):
            flush_paragraph(); close_list(); code = []
            continue
        if not line.strip():
            flush_paragraph(); close_list(); continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph(); close_list()
            level = min(len(heading.group(1)) + 1, 6)
            emit(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        if line.strip() in {"---", "***", "___"}:
            flush_paragraph(); close_list(); emit("<hr>"); continue
        if line.startswith("> "):
            flush_paragraph(); close_list(); emit(f"<blockquote>{_inline(line[2:])}</blockquote>"); continue
        unordered = _UL_RE.match(line)
        ordered = _OL_RE.match(line)
        if unordered or ordered:
            flush_paragraph()
            wanted = "ul" if unordered else "ol"
            if list_kind != wanted:
                close_list(); emit(f"<{wanted}>"); list_kind = wanted
            emit(f"<li>{_inline((unordered or ordered).group(1))}</li>")
            continue
        close_list()
        paragraph.append(line)

    if code is not None:
        raise RenderRejected("unclosed fenced code block")
    flush_paragraph(); close_list()
    rendered = "".join(output)
    if len(rendered) > config.max_rendered_chars:
        raise DetailCapacityExceeded("rendered body exceeds the configured bound")
    return {"state": "sanitized_html", "html": rendered}


def plain_text_fallback(body: str, *, config: RenderConfig) -> dict[str, str]:
    if len(body) > min(config.max_body_chars, config.max_rendered_chars):
        raise DetailCapacityExceeded("plain-text fallback exceeds the configured bound")
    return {"state": "plain_text_fallback", "text": body}


def _inline(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _LINK_RE.finditer(value):
        pieces.append(html.escape(value[cursor:match.start()], quote=False))
        label = html.escape(match.group(1), quote=False)
        href = _safe_href(match.group(2))
        pieces.append(label if href is None else f'<a href="{html.escape(href, quote=True)}">{label}</a>')
        cursor = match.end()
    pieces.append(html.escape(value[cursor:], quote=False))
    return "".join(pieces)


def _safe_href(raw: str) -> str | None:
    if raw.startswith("//") or "\\" in raw:
        return None
    parsed = urlsplit(raw)
    # Production canonical-origin ownership is still Gate A/3D-gated. Until a
    # canonical origin is injected into this renderer, keep the local Stage 4
    # candidate to normalized same-origin relative references only rather than
    # guessing whether an absolute HTTP(S) URL is same-origin.
    if parsed.scheme or parsed.netloc:
        return None
    if raw.startswith("#"):
        return "/" + raw
    if raw.startswith("/"):
        return raw
    return "/" + raw.lstrip("./")


__all__ = [
    "DetailCapacityExceeded",
    "RenderConfig",
    "RenderRejected",
    "RENDERER_CONTRACT_VERSION",
    "plain_text_fallback",
    "render_body",
]
