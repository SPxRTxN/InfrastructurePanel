"""Безопасный Markdown: markdown-it-py без сырого HTML + санитизация nh3 по белому списку."""
import nh3
from markdown_it import MarkdownIt

# html=False: теги во входном тексте экранируются, а не вставляются
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(
    ["table", "strikethrough"]
)

ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "s", "code", "pre",
    "blockquote", "ul", "ol", "li", "a", "img", "table", "thead", "tbody", "tr", "th", "td",
}
ALLOWED_ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "title"}, "th": {"align"}, "td": {"align"},
                 "ol": {"start"}}


def render_markdown(text: str) -> str:
    html = _md.render(text or "")
    # Второй рубеж: даже если парсер что-то пропустит, nh3 вырежет скрипты, обработчики и javascript:-ссылки
    return nh3.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
                     url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow")
