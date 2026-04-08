# utils/markdown_to_html.py

import re
import logging
import bleach
import markdown2
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# =========================
# SECURITY CONFIG
# =========================

ALLOWED_TAGS = [
    "p","h1","h2","h3","h4","h5","h6",
    "strong","em","del","b","i","u","s","mark",
    "sup","sub","small","kbd",
    "ul","ol","li",
    "a","img",
    "code","pre",
    "blockquote","br","hr",
    "table","thead","tbody","tr","th","td",
    "div","span","figure","figcaption",
    "details","summary",
]

ALLOWED_ATTRIBUTES = {
    "*": ["class", "id", "title", "role", "style"],
    "a": ["href", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["align", "colspan", "rowspan"],
    "th": ["align", "colspan", "rowspan"],
    "details": ["open"],
}
css_sanitizer=None  # Use default CSS sanitizer

# =========================
# URL VALIDATION
# =========================

def is_safe_url(url: str) -> bool:
    try:
        if not url or url.startswith("#") or url.startswith("/"):
            return True

        parsed = urlparse(url)
        allowed_schemes = {"http", "https", "mailto", "tel", "data"}
        return parsed.scheme.lower() in allowed_schemes if parsed.scheme else True
    except Exception as e:
        logger.warning(f"URL validation error: {e}")
        return False

# =========================
# CORE CONVERTER
# =========================

def markdown_to_safe_html(markdown_text: str) -> str:
    if not isinstance(markdown_text, str) or not markdown_text.strip():
        return ""

    try:
        html = markdown2.markdown(
            markdown_text,
            extras=[
                "break-on-newline",
                "fenced-code-blocks",
                "tables",
                "strike",
                "footnotes",
                "task_list",
                "cuddled-lists",
                "header-ids",
                "highlightjs-lang",
                "spoiler",
                "target-blank-links",
            ]
        )

        sanitized_html = bleach.clean(
            html,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols=["http","https","mailto","tel","data"],
            strip=True,
            strip_comments=True,
        )

        sanitized_html = add_noopener_noreferrer(sanitized_html)
        return sanitized_html

    except Exception as e:
        logger.error("Markdown processing error", exc_info=True)
        return "<p>Terjadi kesalahan saat memproses konten.</p>"

# =========================
# POST PROCESSING
# =========================

def add_noopener_noreferrer(html: str) -> str:
    pattern = r'<a([^>]*)target="_blank"([^>]*)>'

    def replace(match):
        tag = match.group(0)
        if 'rel=' in tag:
            rel_match = re.search(r'rel="([^"]*)"', tag)
            if rel_match:
                existing = rel_match.group(1)
                new_rel = _merge_rel(existing, "noopener noreferrer")
                return tag.replace(rel_match.group(0), f'rel="{new_rel}"')
        else:
            return f'<a{match.group(1)}target="_blank"{match.group(2)} rel="noopener noreferrer">'
        return tag

    return re.sub(pattern, replace, html)

def _merge_rel(existing: str, new: str) -> str:
    return " ".join(sorted(set(existing.split()) | set(new.split())))

# =========================
# UTILITIES
# =========================

def extract_text_from_html(html: str) -> str:
    try:
        text = re.sub(r'<[^>]+>', '', html)
        return re.sub(r'\s+', ' ', text).strip()
    except Exception:
        return html

def truncate_html(html: str, max_length: int = 200) -> str:
    text = extract_text_from_html(html)
    if len(text) <= max_length:
        return html
    return f"<p>{bleach.clean(text[:max_length])}...</p>"

def generate_preview(html: str, max_chars: int = 150) -> str:
    text = extract_text_from_html(html)
    if len(text) <= max_chars:
        return text
    words = text[:max_chars].split()
    if len(words) > 1:
        words.pop()
    return " ".join(words) + "..."

def is_valid_html(html: str) -> bool:
    return bool(re.search(r'<[^>]+>', html))

def contains_script_tags(html: str) -> bool:
    return bool(re.search(r'<\s*script\b|on\w+\s*=', html, re.IGNORECASE))

def contains_iframe_tags(html: str) -> bool:
    return bool(re.search(r'<\s*iframe\b', html, re.IGNORECASE))

# =========================
# EXPORT API
# =========================

__all__ = [
    "markdown_to_safe_html",
    "extract_text_from_html",
    "generate_preview",
    "truncate_html",
    "is_valid_html",
    "contains_script_tags",
    "contains_iframe_tags",
    "add_noopener_noreferrer",
    "is_safe_url",
]
