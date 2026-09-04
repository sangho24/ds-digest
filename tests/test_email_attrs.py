"""이메일 HTML 의 속성 무결성.

폰트 스택에 큰따옴표가 들어가 style="..." 이 중간에서 끊긴 사고(2026-09-04)를
막는다. Chrome 은 끊긴 앞부분만 적용해 스크린샷이 멀쩡해 보였지만, Gmail 은
깨진 style 을 통째로 버려 간격·활자가 전부 사라졌다. 따라서 렌더 결과를
파서로 읽어 허용 목록 밖의 속성(끊긴 style 의 잔해)이 하나도 없어야 한다.
"""

import json
from html.parser import HTMLParser
from pathlib import Path

from app.models import DigestItem
from app.newsletter import render_digest_email

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / "data" / "records" / "digest_2026-09-04.json"

ALLOWED_ATTRS = {
    "style", "align", "valign", "width", "height", "bgcolor", "cellpadding",
    "cellspacing", "border", "role", "href", "target", "class", "colspan",
    "lang", "dir", "xmlns", "content", "name", "charset", "http-equiv", "id",
    "type", "title", "media", "rel",
}


class _AttrCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.unknown: list[tuple[str, str]] = []
        self.styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key not in ALLOWED_ATTRS:
                self.unknown.append((tag, key))
            if key == "style" and value is not None:
                self.styles.append(value)


def _render_today() -> str:
    record = json.loads(RECORD.read_text())
    items = [DigestItem(**it) for it in record["items"]]
    return render_digest_email(items)


def test_no_broken_attributes_in_rendered_email():
    parser = _AttrCollector()
    parser.feed(_render_today())
    assert parser.unknown == [], f"끊긴 style 의 잔해로 보이는 속성: {parser.unknown[:10]}"


def test_inline_styles_contain_no_double_quotes():
    parser = _AttrCollector()
    parser.feed(_render_today())
    assert parser.styles, "인라인 style 이 하나도 없다"
    offenders = [s for s in parser.styles if '"' in s]
    assert offenders == [], offenders[:3]


def test_every_style_declaration_is_complete():
    """마지막 선언이 'prop:' 로 끝나며 값이 비는 경우(끊김의 전형)를 잡는다."""
    parser = _AttrCollector()
    parser.feed(_render_today())
    for style in parser.styles:
        for decl in style.split(";"):
            decl = decl.strip()
            if not decl:
                continue
            prop, _, value = decl.partition(":")
            assert value.strip(), f"값이 빈 선언: {decl!r} in {style[:80]!r}"
