"""이메일 전용 템플릿(digest_email.html) 검증.

웹용 digest.html 을 메일로 보냈더니 네이버 메일에서 글자 배열과 디자인이 전부
깨졌다(2026-09-04). 메일 클라이언트가 못 읽는 것(CSS 변수·flex·<details>·외부
폰트·<style> 의존)이 렌더 결과에 없는지, 정답·해설이 맨 아래 섹션으로 잘
모였는지를 고정한다.

렌더 결과를 눈으로 보려면(검증자 스크린샷용, 프로덕션과 같은 경로: date_str 미지정):
  .venv/bin/python -c "import pathlib;from app.models import DigestRecord;from app.newsletter import render_digest_email;r=DigestRecord.model_validate_json(pathlib.Path('data/records/digest_2026-09-04.json').read_text(encoding='utf-8'));pathlib.Path('/private/tmp/claude-501/-Users-sangho-ds-digest/2f1c6bb2-9349-4b0e-803d-bf4d978edd22/scratchpad/email_new.html').write_text(render_digest_email(r.items),encoding='utf-8')"
"""
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

import app.contract
import app.newsletter
from app.models import DigestItem, DigestRecord
from app.newsletter import render_digest_email, render_digest_web, email_subject

ALPHA = ["①", "②", "③", "④"]

RECORD_PATH = Path(__file__).parent.parent / "data" / "records" / "digest_2026-09-04.json"

# 메일 클라이언트가 버리거나 깨뜨리는 것들. 하나라도 있으면 어딘가에서 깨진다.
FORBIDDEN = ["var(--", "display:flex", "display: flex", "<details", "fonts.googleapis", "@import"]


@pytest.fixture(scope="module")
def items() -> list[DigestItem]:
    record = DigestRecord.model_validate_json(RECORD_PATH.read_text(encoding="utf-8"))
    assert len(record.items) == 5, "오늘 레코드는 다섯 항목이어야 한다"
    return record.items


@pytest.fixture(scope="module")
def html(items: list[DigestItem]) -> str:
    return render_digest_email(items, "2026년 09월 04일")


def _strip_style_blocks(markup: str) -> str:
    return re.sub(r"<style\b[^>]*>.*?</style>", "", markup, flags=re.S | re.I)


def test_renders_all_item_titles(items, html):
    """(a) 오늘 레코드로 렌더가 성공하고 다섯 항목 제목이 모두 들어 있다."""
    for item in items:
        assert item.raw.title in html
        assert item.raw.url in html


def test_no_email_unsafe_constructs(html):
    """(b) CSS 변수·flex·<details>·외부 폰트·@import 가 0회."""
    for needle in FORBIDDEN:
        assert html.count(needle) == 0, f"{needle!r} 가 {html.count(needle)}회 들어 있다"
    # 외부 리소스는 이미지 <img> 만 허용. 현재 템플릿에는 이미지가 없으니 <link> 도 없어야 한다.
    assert "<link" not in html.lower()


def test_no_css_margin_and_no_zero_font_bars(html):
    """Gmail 안드로이드는 margin 을 무시하고 font-size:0 셀을 최소 글자 크기로
    키운다(실물 피드백 2026-09-04: 간격 붕괴·계기 막대 어긋남). 간격은 padding 과
    spacer 행으로만, 계기는 글자(●○)로만 그린다."""
    assert "margin" not in html
    assert "font-size:0" not in html
    assert "●" in html and "○" in html
    # 구조적 구분은 실선 테두리로 한다(다크모드 반전에서도 남는다).
    assert html.count("border:1px solid #C9D0D8") >= 5
    # 반전 시 사라지는 아주 밝은 회색 글자는 쓰지 않는다.
    assert "#8B96A2" not in html


def test_table_layout_survives_without_style_block(items, html):
    """(c) 테이블 레이아웃이고, <style> 을 전부 제거해도 제목·요약 텍스트가 남는다."""
    assert "<table" in html
    assert 'role="presentation"' in html
    stripped = _strip_style_blocks(html)
    assert "<style" not in stripped
    for item in items:
        assert item.raw.title in stripped
        assert item.analysis.one_line_summary in stripped
    # 스타일은 인라인으로 남아 있어야 한다(색·폰트가 <style> 에만 있으면 안 된다).
    assert 'style="' in stripped
    assert "#131A22" in stripped and "#FFFFFF" in stripped


def test_answer_section_matches_quiz_items(items, html):
    """(d) 퀴즈가 있는 항목 수 == 정답 섹션의 항목 묶음 수, 총 문항 수 == 정답 수."""
    items_with_quiz = [i for i in items if i.analysis.quiz]
    total_questions = sum(len(i.analysis.quiz) for i in items)
    assert items_with_quiz, "이 테스트는 퀴즈가 있는 레코드를 전제한다"

    assert "정답과 해설" in html
    assert html.count('class="answer-item"') == len(items_with_quiz)
    assert html.count('class="answer"') == total_questions
    assert html.count('class="quiz-q"') == total_questions
    # 문항 번호(Q1..Qn)가 문항 쪽과 정답 쪽에 각각 한 번씩 이어진다.
    for n in range(1, total_questions + 1):
        assert html.count(f"Q{n}.") == 2, f"Q{n} 번호가 문항·정답에 한 번씩 있어야 한다"
    # 항목 쪽에는 정답이 노출되지 않고 안내만 있다.
    assert "정답과 해설은 맨 아래에 있습니다" in html
    for item in items_with_quiz:
        for q in item.analysis.quiz:
            if q.explanation:
                assert html.count(q.explanation) == 1, "해설은 정답 섹션에만 한 번 나와야 한다"


def test_answer_section_marks_the_correct_option(items, html):
    """정답 섹션이 **어느 선택지**를 정답으로 표시하는지 검사한다.

    answer_index 를 0 으로 고정하는 변이가 이전 테스트를 통과했다(검증 지적).
    문항마다 `q.options[q.answer_index]` 가 정답 줄에 있고, 오답 선택지는
    정답 표시로 나오지 않아야 한다.
    """
    section = html.split("정답과 해설", 1)[1]
    n = 0
    for item in items:
        for q in item.analysis.quiz:
            n += 1
            correct = f"Q{n}. {ALPHA[q.answer_index]} {q.options[q.answer_index]}"
            assert correct in section, f"Q{n} 정답 줄이 없다: {correct!r}"
            if q.explanation:
                assert q.explanation in section
            for i, opt in enumerate(q.options):
                if i != q.answer_index:
                    wrong = f"Q{n}. {ALPHA[i]} {opt}"
                    assert wrong not in section, f"Q{n} 오답이 정답으로 표시됨: {wrong!r}"
    assert n == section.count('class="answer"')


class _FixedDatetime(datetime):
    """app.contract 의 datetime 대체. now(tz) 가 고정 UTC 시각을 tz 로 변환해 돌려준다."""
    FIXED_UTC = datetime(2026, 9, 3, 21, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.FIXED_UTC.astimezone(tz) if tz else cls.FIXED_UTC.replace(tzinfo=None)


class _UtcDate(date):
    """app.newsletter 의 date 대체. today() 가 UTC 날짜(하루 전)를 돌려준다."""

    @classmethod
    def today(cls):
        return cls(2026, 9, 3)


def test_dates_follow_kst_not_utc_runner(items, monkeypatch):
    """cron 이 KST 04:37 로 옮겨져 실행 시각이 UTC 전날 21~22시다. 이때 머리말·
    <title>·subject·아카이브 링크·웹 머리말이 전부 KST 오늘(09-04)이어야 한다.
    `date.today()` 를 쓰면 09-03 이 나온다."""
    monkeypatch.setattr(app.contract, "datetime", _FixedDatetime)
    monkeypatch.setattr(app.newsletter, "date", _UtcDate)
    assert app.contract.today_kst() == "2026-09-04"
    assert _UtcDate.today().isoformat() == "2026-09-03", "UTC 날짜 고정이 안 걸렸다"

    email = render_digest_email(items)
    assert "2026년 09월 04일" in email
    assert "<title>DS Digest - 2026년 09월 04일</title>" in email
    assert "https://sangho24.github.io/ds-digest/2026-09-04.html" in email
    assert "2026년 09월 03일" not in email
    assert "2026-09-03" not in email

    assert email_subject(len(items)) == f"[DS Digest 09/04] 오늘의 큐레이션 {len(items)}건"

    web = render_digest_web(items)
    assert "2026년 09월 04일" in web
    assert "2026년 09월 03일" not in web

    # 같은 뿌리의 Telegram 헤더 날짜
    from app.deliverers.telegram import _format_header
    header = _format_header(items)
    assert "9월 4일" in header
    assert "9월 3일" not in header


def test_no_answer_section_without_quiz(items):
    """(e) 퀴즈가 하나도 없으면 정답 섹션이 생기지 않는다."""
    quizless = [
        item.model_copy(update={"analysis": item.analysis.model_copy(update={"quiz": []})})
        for item in items
    ]
    assert all(not i.analysis.quiz for i in quizless)
    html = render_digest_email(quizless, "2026년 09월 04일")
    assert "정답과 해설" not in html
    assert 'class="answer' not in html
    assert "퀴즈" not in html
    # 항목 자체는 그대로 렌더된다.
    for item in quizless:
        assert item.raw.title in html


def test_footer_links_to_archive(html):
    """푸터는 아카이브 링크와 자동 발송 안내를 담고, Discord 문구는 없다."""
    assert "https://sangho24.github.io/ds-digest/" in html
    assert "이 메일은 매일 아침 자동 발송됩니다" in html
    assert "Discord" not in html


def test_archive_url_override():
    """archive_url 을 넘기면 그 값이 쓰인다."""
    record = DigestRecord.model_validate_json(RECORD_PATH.read_text(encoding="utf-8"))
    html = render_digest_email(record.items, archive_url="https://example.test/2026-09-04.html")
    assert "https://example.test/2026-09-04.html" in html


class _CellText(HTMLParser):
    """가장 안쪽 <td> 를 기준으로 (행 번호, 셀 텍스트) 를 모은다.

    바깥 td 에는 안쪽 td 의 텍스트를 넣지 않는다. 중첩 표가 많은 문서라
    "같은 칸/같은 행에 있는가"를 이 단위로 판정해야 한다.
    """

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[int] = []      # 열려 있는 <tr> 의 번호 스택
        self._cells: list[list[str]] = []  # 열려 있는 <td> 의 텍스트 버퍼 스택
        self._row_seq = 0
        self.cells: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row_seq += 1
            self._rows.append(self._row_seq)
        elif tag == "td":
            self._cells.append([])

    def handle_endtag(self, tag):
        if tag == "tr" and self._rows:
            self._rows.pop()
        elif tag == "td" and self._cells:
            text = "".join(self._cells.pop())
            self.cells.append((self._rows[-1] if self._rows else -1, text))

    def handle_data(self, data):
        if self._cells:
            self._cells[-1].append(data)


def _label_rows(html: str, label: str) -> set[int]:
    """셀 텍스트가 정확히 그 라벨인 칸이 들어 있는 행 번호들."""
    parser = _CellText()
    parser.feed(html)
    return {row for row, text in parser.cells if text.strip() == label}


def test_action_items_label_replaces_to_do(items, html):
    """"To do" 라벨을 "Action Items" 로 바꿨다. 표시는 text-transform 으로
    대문자가 되지만, 그 CSS 를 버리는 클라이언트도 있으니 원문으로 단언한다."""
    with_ideas = [i for i in items if i.analysis.production_ideas]
    assert with_ideas, "이 테스트는 production_ideas 가 있는 레코드를 전제한다"

    assert html.count("Action Items") == len(with_ideas)
    for dead in ("To do", "TO DO", "To&nbsp;do"):
        assert dead not in html, f"{dead!r} 가 남아 있다"
    # 대문자 변환은 인라인 style 로 걸려 있어야 한다(라벨 칸이 uppercase).
    assert "text-transform:uppercase" in html


def test_two_axes_are_on_separate_rows(items, html):
    """실행가능성과 깊이가 각각 다른 <tr> 에 있고, 한 칸에 같이 있지 않다."""
    action_rows = _label_rows(html, "실행가능성")
    depth_rows = _label_rows(html, "깊이")

    assert len(action_rows) == len(items), "항목마다 실행가능성 라벨 칸이 하나씩 있어야 한다"
    assert len(depth_rows) == len(items), "항목마다 깊이 라벨 칸이 하나씩 있어야 한다"
    assert not (action_rows & depth_rows), "두 축이 같은 행에 있다"

    # 같은 칸에 두 라벨이 함께 있으면 안 된다(한 줄로 붙여 놓은 예전 모양).
    parser = _CellText()
    parser.feed(html)
    for _, text in parser.cells:
        assert not ("실행가능성" in text and "깊이" in text), f"한 칸에 두 축이 같이 있다: {text[:60]!r}"

    # 점 표기(●○)와 숫자는 그대로 유지한다.
    gauge_cells = [text for _, text in parser.cells if "●" in text or "○" in text]
    assert len(gauge_cells) == len(items) * 2, "항목마다 두 축의 계기 칸이 있어야 한다"
    for text in gauge_cells:
        assert text.count("●") + text.count("○") == 10, f"눈금 10칸이 아니다: {text!r}"
