# Reply formatting per channel.
#
# The model writes every reply ONCE, in a small Markdown subset (see
# build_format_base_instructions in infra/profile.py):
#   **bold**   - bullet lines   1. numbered lines   [label](url)   line breaks
#
# Each channel then shows it in its own way:
#   webchat   stored as-is; the Angular widget renders it to sanitized HTML
#   whatsapp  converted here to WhatsApp's syntax (*bold*, bullets, bare URLs)
#
# Asking the model for HTML instead would put model output straight into a
# browser's DOM and mean nothing on WhatsApp, which shows tags literally.
import re

_BOLD = re.compile(r'\*\*(.+?)\*\*', re.S)
_BOLD_UNDERSCORE = re.compile(r'__(.+?)__', re.S)
_LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)')
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.*)$', re.M)
_BULLET = re.compile(r'^(\s*)[-*+]\s+', re.M)
_CODE_TICKS = re.compile(r'`([^`\n]+)`')


def to_whatsapp(text: str | None) -> str:
    """Markdown subset -> WhatsApp formatting. Plain text passes through."""
    if not text:
        return text or ''
    out = text
    # headings are not part of the contract, but a model slips one in now and
    # then - WhatsApp has none, so it becomes a bold line
    out = _HEADING.sub(lambda m: f'**{m.group(1).strip()}**', out)
    # bullets BEFORE bold: a "* item" bullet must not be read as emphasis
    out = _BULLET.sub(lambda m: f'{m.group(1)}• ', out)
    out = _LINK.sub(lambda m: f'{m.group(1)}: {m.group(2)}', out)
    out = _BOLD.sub(lambda m: f'*{m.group(1)}*', out)
    out = _BOLD_UNDERSCORE.sub(lambda m: f'*{m.group(1)}*', out)
    out = _CODE_TICKS.sub(lambda m: m.group(1), out)
    return out
