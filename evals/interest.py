"""관심도 표시(interest labeling): 다이제스트 아이템을 사용자가 "읽고 싶다 / 아니다 / 모르겠다" 로 표시한다.

왜 필요한가:
    계기판은 지금까지 모양 지표(길이·태그 분포·근거 여부)만 쟀고, "사용자가 실제로 관심 있는가" 를
    재는 숫자가 없었다. 이 모듈은 정본 `data/records/digest_*.json` 의 아이템을 HTML 한 장에 싣고,
    사람이 표시한 결과를 jsonl 로 반입해 출처·소스 유형·모델 relevance_score 구간별 "읽고 싶다" 비율을
    낸다. 점수 구간별 비율이 핵심이다: 모델이 높게 매긴 것을 사용자도 읽고 싶어 하는지 본다.

범위:
    보고 전용이다. evals/judge.py 와는 독립이다(심판은 근거 검증, 여기는 취향 측정).
    페이지에는 모델이 매긴 점수(relevance_score, domain 등)를 보이지 않는다. 사람 판단이 모델 점수에
    끌려가면 안 되기 때문이다. data-* 속성에만 실어 둔다.

실행 예:
    .venv/bin/python -m evals.interest page --days 14
    .venv/bin/python -m evals.interest page --since 2026-10-20 --until 2026-10-31 --out /tmp/interest.html
    .venv/bin/python -m evals.interest import-labels labels.json
    .venv/bin/python -m evals.interest report
    .venv/bin/python -m evals.interest report --json

사람 표시 흐름:
    page(HTML 한 장, 브라우저에서 표시, localStorage 자동 저장) → "결과 JSON 복사" →
    import-labels(data/evals/interest_labels.jsonl 에 append) → report.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import html
import json
from pathlib import Path
import sys
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = ROOT / "data" / "records"
DEFAULT_PAGE = ROOT / "data" / "evals" / "interest.html"
DEFAULT_LABELS = ROOT / "data" / "evals" / "interest_labels.jsonl"

# 사람 표시 값. 페이지의 라디오 값이자 interest_labels.jsonl 의 human 필드다.
HUMAN_CHOICES = ("want", "skip", "unsure")
HUMAN_LABELS_KO = {"want": "읽고 싶다", "skip": "아니다", "unsure": "모르겠다"}
# 라벨 JSON 에서 그대로 옮겨 적는 필드. 페이지 JSON 모양과 같다.
LABEL_KEY_FIELDS = ("date", "title", "source_key", "source_type")
# relevance_score 구간. (라벨, 하한, 상한) 순이며 상한 포함.
SCORE_BANDS = (("0~5", 0, 5), ("6~7", 6, 7), ("8~10", 8, 10))
SCORE_BAND_UNKNOWN = "미상"
MAX_KEY_POINTS = 3
MAX_TAGS = 5
MAX_TITLES = 20
MAX_NOTE_LEN = 200
DEFAULT_DAYS = 14


# ──────────────────────────────────────────────
# 정본 읽기
# ──────────────────────────────────────────────


def _parse_date(text: str | None) -> date | None:
    """YYYY-MM-DD 문자열을 date 로. 형식이 틀리면 None."""
    if not text:
        return None
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _date_of_file(path: Path, payload: dict[str, Any]) -> date | None:
    """정본 날짜: payload 의 date 를 우선, 없으면 파일명 digest_YYYY-MM-DD.json 에서."""
    return _parse_date(payload.get("date")) or _parse_date(path.stem.replace("digest_", ""))


def _key_point_texts(raw_points: Any) -> list[str]:
    """key_points 는 {"point": str, "timestamp": ...} 목록이거나 문자열 목록이다. 문자열만 뽑는다."""
    texts: list[str] = []
    if not isinstance(raw_points, list):
        return texts
    for entry in raw_points:
        if isinstance(entry, dict):
            text = entry.get("point")
        else:
            text = entry
        if text is None:
            continue
        text = str(text).strip()
        if text:
            texts.append(text)
    return texts


def _str_list(value: Any) -> list[str]:
    """문자열 목록으로 정규화. None·단일 문자열·빈 항목을 방어한다."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _coerce_score(value: Any) -> int | None:
    """relevance_score 를 정수로. 숫자가 아니면 None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def flatten_item(entry: dict[str, Any], record_date: date, position: int) -> dict[str, Any] | None:
    """정본 아이템 하나를 페이지·집계가 쓰는 평평한 dict 로. url 이 없으면 None(식별 불가)."""
    raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}
    analysis = entry.get("analysis") if isinstance(entry.get("analysis"), dict) else {}
    url = str(raw.get("url") or "").strip()
    if not url:
        return None
    source_label = str(raw.get("source_label") or raw.get("source_name") or "").strip()
    return {
        "url": url,
        "date": record_date.isoformat(),
        "position": position,
        "title": str(raw.get("title") or "").strip() or "(제목 없음)",
        "source_key": str(raw.get("source_key") or "").strip(),
        "source_type": str(raw.get("source_type") or "").strip() or "unknown",
        "source_label": source_label or "(출처 미상)",
        "published_at": raw.get("published_at"),
        "summary": str(analysis.get("one_line_summary") or "").strip(),
        "key_points": _key_point_texts(analysis.get("key_points")),
        "tags": _str_list(analysis.get("tags")),
        "relevance_score": _coerce_score(analysis.get("relevance_score")),
        "domain": _str_list(analysis.get("domain")),
    }


def iter_record_files(records_dir: Path, since: date | None = None, until: date | None = None):
    """기간 안의 (날짜, 경로) 를 날짜 내림차순으로 낸다. 날짜를 못 읽는 파일은 건너뛴다."""
    found: list[tuple[date, Path]] = []
    if not records_dir.exists():
        return found
    for path in sorted(records_dir.glob("digest_*.json")):
        record_date = _parse_date(path.stem.replace("digest_", ""))
        if record_date is None:
            continue
        if since and record_date < since:
            continue
        if until and record_date > until:
            continue
        found.append((record_date, path))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return found


def load_items(
    records_dir: Path | str | None = None, since: date | None = None, until: date | None = None
) -> list[dict[str, Any]]:
    """기간 내 정본 아이템을 최신 날짜 먼저, 날짜 안에서는 파일 순서대로. 같은 url 은 첫 것만 남긴다."""
    base = Path(records_dir) if records_dir is not None else RECORDS_DIR
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record_date, path in iter_record_files(base, since, until):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        actual_date = _date_of_file(path, payload) or record_date
        for position, entry in enumerate(payload.get("items") or []):
            if not isinstance(entry, dict):
                continue
            item = flatten_item(entry, actual_date, position)
            if item is None or item["url"] in seen:
                continue
            seen.add(item["url"])
            items.append(item)
    return items


# ──────────────────────────────────────────────
# HTML 페이지
# ──────────────────────────────────────────────

_PAGE_CSS = """
:root{--bg:#fff;--fg:#1b1b1b;--muted:#666;--line:#ddd;--box:#f6f7f9;--accent:#2563eb;--chip:#eef1f5}
@media(prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e8e8e8;--muted:#9a9a9a;--line:#333;--box:#1c2026;--accent:#60a5fa;--chip:#262b33}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}
main{max-width:820px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.lead{color:var(--muted);margin:0 0 16px;font-size:14px}
.header{margin:0 0 6px;font-size:14px;font-weight:600;word-break:break-word}
.card{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 14px;outline:none}
.card:focus-within,.card:focus{border-color:var(--accent)}
.meta{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:6px 10px}
.meta .type{text-transform:uppercase;letter-spacing:.02em}
h2{font-size:17px;margin:6px 0 4px;line-height:1.35;word-break:break-word}
h2 a{color:inherit;text-decoration:none}
h2 a:hover{text-decoration:underline}
.summary{font-weight:600;margin:0 0 6px}
ul.points{margin:0 0 8px;padding-left:20px}
ul.points li{margin:2px 0;font-size:14px}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 4px}
.tag{font-size:12px;padding:1px 8px;border-radius:10px;background:var(--chip);color:var(--muted)}
.human{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin-top:10px;padding-top:10px;border-top:1px dashed var(--line)}
.human label{white-space:nowrap;cursor:pointer}
.human kbd{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:0 4px;margin-left:2px}
.note{flex:1 1 200px;min-width:0;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit;font-size:14px}
.foot{position:sticky;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 0;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{padding:8px 14px;border:1px solid var(--line);border-radius:6px;background:var(--box);color:var(--fg);cursor:pointer;font:inherit}
#status{color:var(--muted);font-size:14px}
#fallback{width:100%;min-height:140px;font:13px/1.4 ui-monospace,Menlo,monospace;display:none;margin-top:8px}
.empty{padding:40px 0;text-align:center;color:var(--muted)}
@media(max-width:480px){body{padding:10px}.card{padding:10px 12px}h2{font-size:16px}}
"""

_PAGE_JS = """
(function(){
  var KEY='interest:';
  var cards=Array.prototype.slice.call(document.querySelectorAll('.card'));
  var status=document.getElementById('status');
  var fallback=document.getElementById('fallback');
  var KEYMAP={'1':'want','2':'skip','3':'unsure'};
  function save(url,value){try{localStorage.setItem(KEY+url,JSON.stringify(value));}catch(e){}}
  function restore(url){try{var v=localStorage.getItem(KEY+url);return v?JSON.parse(v):null;}catch(e){return null;}}
  function readCard(card){
    var checked=card.querySelector('input[type=radio]:checked');
    var note=card.querySelector('.note');
    return {human:checked?checked.value:'',note:note?note.value.trim().slice(0,200):''};
  }
  function collect(){
    var out=[];
    cards.forEach(function(card){
      var r=readCard(card);
      if(!r.human) return;
      out.push({url:card.getAttribute('data-url'),date:card.getAttribute('data-date'),
        title:card.getAttribute('data-title'),source_key:card.getAttribute('data-source-key'),
        source_type:card.getAttribute('data-source-type'),human:r.human,note:r.note});
    });
    return out;
  }
  function updateStatus(){ if(status) status.textContent='표시 '+collect().length+' / '+cards.length+'건'; }
  cards.forEach(function(card){
    var url=card.getAttribute('data-url');
    var saved=restore(url);
    if(saved&&typeof saved==='object'){
      if(saved.human){var r=card.querySelector('input[type=radio][value="'+saved.human+'"]'); if(r) r.checked=true;}
      var n=card.querySelector('.note'); if(n&&saved.note) n.value=String(saved.note);
    }
    card.addEventListener('change',function(){ save(url,readCard(card)); updateStatus(); });
    card.addEventListener('input',function(){ save(url,readCard(card)); });
    card.addEventListener('keydown',function(e){
      if(e.target&&e.target.tagName==='INPUT'&&e.target.type==='text') return;
      var v=KEYMAP[e.key]; if(!v) return;
      var r=card.querySelector('input[type=radio][value="'+v+'"]');
      if(r){ r.checked=true; save(url,readCard(card)); updateStatus(); e.preventDefault(); }
    });
  });
  updateStatus();
  var btn=document.getElementById('copy');
  if(btn) btn.addEventListener('click',function(){
    var rows=collect();
    var text=JSON.stringify(rows,null,2);
    function showFallback(){
      fallback.style.display='block';fallback.value=text;fallback.focus();fallback.select();
      status.textContent='클립보드 복사가 막혀 아래 상자에 넣었다. 전체 선택해 복사하라.';
    }
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(function(){
        status.textContent='결과 JSON '+rows.length+'건을 클립보드에 복사했다. import-labels 로 반입하라.';
      },showFallback);
    }else{showFallback();}
  });
})();
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _radio_html(index: int) -> str:
    """선택지 라디오 3개. name 은 카드 순번으로 만들어 충돌이 없다."""
    name = f"h-{index}"
    parts = []
    for hotkey, choice in enumerate(HUMAN_CHOICES, start=1):
        parts.append(
            f'<label><input type="radio" name="{name}" value="{choice}"> '
            f"{HUMAN_LABELS_KO[choice]}<kbd>{hotkey}</kbd></label>"
        )
    return "".join(parts)


def _card_html(item: dict[str, Any], index: int) -> str:
    """카드 하나. 모델 점수(relevance_score, domain)는 보이는 텍스트에 넣지 않고 data-* 에만 둔다."""
    points = item.get("key_points") or []
    points_html = (
        "<ul class=\"points\">" + "".join(f"<li>{_esc(p)}</li>" for p in points[:MAX_KEY_POINTS]) + "</ul>"
        if points
        else ""
    )
    tags = item.get("tags") or []
    tags_html = (
        '<div class="tags">' + "".join(f'<span class="tag">{_esc(t)}</span>' for t in tags[:MAX_TAGS]) + "</div>"
        if tags
        else ""
    )
    summary = item.get("summary")
    summary_html = f'<p class="summary">{_esc(summary)}</p>' if summary else ""
    score = item.get("relevance_score")
    score_attr = "" if score is None else str(score)
    domain_attr = ",".join(item.get("domain") or [])
    return (
        f'<section class="card" tabindex="0" data-url="{_esc(item["url"])}" data-date="{_esc(item["date"])}"'
        f' data-title="{_esc(item["title"])}" data-source-key="{_esc(item["source_key"])}"'
        f' data-source-type="{_esc(item["source_type"])}" data-score="{_esc(score_attr)}"'
        f' data-domain="{_esc(domain_attr)}">'
        f'<div class="meta"><span>{_esc(item["date"])}</span><span>{_esc(item["source_label"])}</span>'
        f'<span class="type">{_esc(item["source_type"])}</span></div>'
        f'<h2><a href="{_esc(item["url"])}" target="_blank" rel="noopener">{_esc(item["title"])}</a></h2>'
        f"{summary_html}{points_html}{tags_html}"
        f'<div class="human">{_radio_html(index)}'
        f'<input class="note" type="text" maxlength="{MAX_NOTE_LEN}" placeholder="한 줄 메모(선택)"></div>'
        "</section>"
    )


def build_page(items: list[dict[str, Any]], header: str | None = None) -> str:
    """아이템 목록을 HTML 한 장으로. 비어 있으면 안내문만 싣는다."""
    if items:
        body = "".join(_card_html(item, index) for index, item in enumerate(items))
        foot = (
            '<div class="foot"><button id="copy" type="button">결과 JSON 복사</button>'
            '<span id="status"></span><textarea id="fallback" readonly></textarea></div>'
        )
    else:
        body = '<p class="empty">기간 안에 정본 아이템이 없다. --days 를 늘리거나 --since 를 앞당겨 보라.</p>'
        foot = ""
    header_html = f'<p class="header">{_esc(header)}</p>' if header else ""
    dates = sorted({item["date"] for item in items})
    span = f" ({dates[0]} ~ {dates[-1]})" if dates else ""
    return (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>다이제스트 관심도 표시</title>"
        f"<style>{_PAGE_CSS}</style></head><body><main>"
        "<h1>다이제스트 관심도 표시</h1>"
        f"{header_html}"
        f'<p class="lead">아이템 {len(items)}건{span}. 제목과 요약만 보고 "지금 읽고 싶은가" 를 표시한다.'
        " 표시는 이 브라우저에 자동 저장되며, 다 되면 하단에서 결과 JSON 을 복사해"
        " <code>python -m evals.interest import-labels</code> 로 반입한다. 카드에 포커스를 두고 1/2/3 키로도 고를 수 있다.</p>"
        f"{body}{foot}"
        f"</main><script>{_PAGE_JS}</script></body></html>\n"
    )


# ──────────────────────────────────────────────
# 라벨 반입·읽기
# ──────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_label(raw: Any) -> dict[str, Any] | None:
    """라벨 한 행을 검증·정규화. url 이 없거나 human 이 세 값 밖이면 None."""
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip()
    human = raw.get("human")
    if not url or human not in HUMAN_CHOICES:
        return None
    record: dict[str, Any] = {
        "url": url,
        "human": human,
        "note": str(raw.get("note") or "").strip()[:MAX_NOTE_LEN],
    }
    for field in LABEL_KEY_FIELDS:
        value = raw.get(field)
        record[field] = None if value is None else str(value)
    return record


def resolve_labels_path(path: Path | str | None) -> Path:
    return Path(path) if path is not None else DEFAULT_LABELS


def import_labels(source: Path | str, labels_path: Path | str | None = None) -> dict[str, int]:
    """페이지에서 복사한 JSON(목록 또는 {"labels": [...]})을 jsonl 에 append 한다. 반환 {imported, rejected}."""
    payload = json.loads(Path(source).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("labels")
    if not isinstance(payload, list):
        raise ValueError('라벨 JSON 은 목록이거나 {"labels": [...]} 여야 한다')
    target = resolve_labels_path(labels_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    imported = rejected = 0
    labeled_at = _now_iso()
    with target.open("a", encoding="utf-8") as handle:
        for raw in payload:
            record = normalize_label(raw)
            if record is None:
                rejected += 1
                continue
            record["labeled_at"] = labeled_at
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            imported += 1
    return {"imported": imported, "rejected": rejected}


def load_labels(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """jsonl 을 읽어 url → 마지막 레코드. 같은 url 이 여러 번이면 뒤의 것이 이긴다."""
    labels_path = resolve_labels_path(path)
    latest: dict[str, dict[str, Any]] = {}
    if not labels_path.exists():
        return latest
    with labels_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            record = normalize_label(raw)
            if record is None:
                continue
            if isinstance(raw, dict) and raw.get("labeled_at"):
                record["labeled_at"] = raw["labeled_at"]
            latest[record["url"]] = record
    return latest


# ──────────────────────────────────────────────
# 집계
# ──────────────────────────────────────────────


def score_band(score: int | None) -> str:
    """relevance_score 를 구간 라벨로. 없거나 범위 밖이면 '미상'."""
    if score is None:
        return SCORE_BAND_UNKNOWN
    for label, low, high in SCORE_BANDS:
        if low <= score <= high:
            return label
    return SCORE_BAND_UNKNOWN


def _ratio(want: int, skip: int) -> float | None:
    """읽고 싶다 비율 = want / (want + skip). 분모가 0 이면 None."""
    denominator = want + skip
    return round(want / denominator, 3) if denominator else None


def _iso_week(text: str | None) -> str:
    parsed = _parse_date(text)
    if parsed is None:
        return "미상"
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def _group_rows(
    counts: dict[str, Counter], min_count: int = 1, order: list[str] | None = None
) -> list[dict[str, Any]]:
    """그룹별 Counter(want/skip/unsure) 를 행 목록으로. order 가 없으면 건수 내림차순."""
    rows = []
    for key, counter in counts.items():
        total = sum(counter.values())
        if total < min_count:
            continue
        rows.append(
            {
                "key": key,
                "count": total,
                "want": counter["want"],
                "skip": counter["skip"],
                "unsure": counter["unsure"],
                "want_ratio": _ratio(counter["want"], counter["skip"]),
            }
        )
    if order is not None:
        rank = {key: index for index, key in enumerate(order)}
        rows.sort(key=lambda row: rank.get(row["key"], len(rank)))
    else:
        rows.sort(key=lambda row: (-row["count"], row["key"]))
    return rows


def summarize(labels: dict[str, dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    """라벨(url → 마지막 레코드)과 정본 아이템을 합쳐 집계한다. unsure 는 비율 분모에서 뺀다."""
    by_url = {item["url"]: item for item in items}
    totals: Counter = Counter()
    by_type: dict[str, Counter] = defaultdict(Counter)
    by_label: dict[str, Counter] = defaultdict(Counter)
    by_band: dict[str, Counter] = defaultdict(Counter)
    by_week: dict[str, Counter] = defaultdict(Counter)
    want_titles: list[dict[str, Any]] = []
    skip_titles: list[dict[str, Any]] = []
    for url, label in labels.items():
        human = label.get("human")
        if human not in HUMAN_CHOICES:
            continue
        item = by_url.get(url) or {}
        totals[human] += 1
        source_type = label.get("source_type") or item.get("source_type") or "unknown"
        by_type[source_type][human] += 1
        by_label[item.get("source_label") or "(정본 없음)"][human] += 1
        by_band[score_band(item.get("relevance_score"))][human] += 1
        label_date = label.get("date") or item.get("date")
        by_week[_iso_week(label_date)][human] += 1
        entry = {
            "date": label_date,
            "title": label.get("title") or item.get("title") or "(제목 없음)",
            "url": url,
            "note": label.get("note") or "",
        }
        if human == "want":
            want_titles.append(entry)
        elif human == "skip":
            skip_titles.append(entry)
    by_date_desc = lambda row: (row["date"] or "", row["title"])  # noqa: E731
    want_titles.sort(key=by_date_desc, reverse=True)
    skip_titles.sort(key=by_date_desc, reverse=True)
    band_order = [label for label, _, _ in SCORE_BANDS] + [SCORE_BAND_UNKNOWN]
    return {
        "total": sum(totals.values()),
        "want": totals["want"],
        "skip": totals["skip"],
        "unsure": totals["unsure"],
        "want_ratio": _ratio(totals["want"], totals["skip"]),
        "by_source_type": _group_rows(by_type, min_count=2),
        "by_source_label": _group_rows(by_label, min_count=2),
        "by_score_band": _group_rows(by_band, order=band_order),
        "by_week": _group_rows(by_week, order=sorted(by_week)),
        "want_titles": want_titles[:MAX_TITLES],
        "skip_titles": skip_titles[:MAX_TITLES],
    }


def _pct(ratio: float | None) -> str:
    return "-" if ratio is None else f"{ratio * 100:.0f}%"


def _format_rows(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["  (2건 이상인 그룹 없음)"]
    return [
        f"  {row['key']}: 읽고 싶다 {_pct(row['want_ratio'])} "
        f"({row['want']}/{row['want'] + row['skip']}, 모르겠다 {row['unsure']}, 총 {row['count']}건)"
        for row in rows
    ]


def format_report(summary: dict[str, Any]) -> str:
    """집계를 사람이 읽는 텍스트로."""
    lines = [
        "다이제스트 관심도 보고",
        f"전체: 표시 {summary['total']}건, 읽고 싶다 {_pct(summary['want_ratio'])} "
        f"(읽고 싶다 {summary['want']} / 아니다 {summary['skip']} / 모르겠다 {summary['unsure']})",
        "",
        "소스 유형별 (2건 이상):",
        *_format_rows(summary["by_source_type"]),
        "",
        "출처별 (2건 이상):",
        *_format_rows(summary["by_source_label"]),
        "",
        "모델 relevance_score 구간별 (높을수록 읽고 싶다 비율이 올라가야 점수가 취향과 맞는 것):",
        *(_format_rows(summary["by_score_band"]) if summary["by_score_band"] else ["  (표시 없음)"]),
        "",
        "주차별 (ISO 주):",
        *(_format_rows(summary["by_week"]) if summary["by_week"] else ["  (표시 없음)"]),
        "",
        f"읽고 싶다 제목 (최대 {MAX_TITLES}개):",
        *([f"  [{t['date'] or '----'}] {t['title']}" for t in summary["want_titles"]] or ["  (없음)"]),
        "",
        f"아니다 제목 (최대 {MAX_TITLES}개):",
        *([f"  [{t['date'] or '----'}] {t['title']}" for t in summary["skip_titles"]] or ["  (없음)"]),
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────


def _resolve_window(args: argparse.Namespace, today: date | None = None) -> tuple[date | None, date | None]:
    """--since/--until 이 있으면 그대로, 없으면 오늘부터 --days 일(오늘 포함)."""
    today = today or date.today()
    since = _parse_date(args.since) if args.since else None
    until = _parse_date(args.until) if args.until else None
    if since is None and until is None and args.days:
        until = today
        since = today - timedelta(days=max(args.days, 1) - 1)
    elif since is None and until is not None and args.days:
        since = until - timedelta(days=max(args.days, 1) - 1)
    return since, until


def _cmd_page(args: argparse.Namespace, out: TextIO) -> int:
    since, until = _resolve_window(args)
    records_dir = Path(args.records_dir) if args.records_dir else RECORDS_DIR
    items = load_items(records_dir, since, until)
    span = " ~ ".join(d.isoformat() for d in (since, until) if d)
    header = f"기간 {span}" if span else None
    page = build_page(items, header=header)
    out_path = Path(args.out) if args.out else DEFAULT_PAGE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    if items:
        print(f"아이템 {len(items)}건을 실었다 → {out_path}", file=out)
    else:
        print(f"기간 안에 정본 아이템이 없다(빈 페이지를 썼다) → {out_path}", file=out)
        print("정본 디렉터리나 --since/--days 를 확인하라.", file=out)
    return 0


def _cmd_import(args: argparse.Namespace, out: TextIO) -> int:
    try:
        result = import_labels(args.source, args.labels)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"반입 실패: {exc}", file=sys.stderr)
        return 1
    target = resolve_labels_path(args.labels)
    print(f"반입 {result['imported']}건 (거부 {result['rejected']}건) → {target}", file=out)
    return 0


def _cmd_report(args: argparse.Namespace, out: TextIO) -> int:
    labels = load_labels(args.labels)
    records_dir = Path(args.records_dir) if args.records_dir else RECORDS_DIR
    items = load_items(records_dir)
    summary = summarize(labels, items)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2), file=out)
    else:
        print(format_report(summary), file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.interest", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    page = sub.add_parser("page", help="관심도 표시 HTML 한 장을 만든다")
    page.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"오늘부터 거슬러 올라갈 일수(기본 {DEFAULT_DAYS})")
    page.add_argument("--since", help="시작 날짜 YYYY-MM-DD(주면 --days 무시)")
    page.add_argument("--until", help="끝 날짜 YYYY-MM-DD")
    page.add_argument("--out", help=f"출력 경로(기본 {DEFAULT_PAGE})")
    page.add_argument("--records-dir", help=argparse.SUPPRESS)
    page.set_defaults(func=_cmd_page)

    imp = sub.add_parser("import-labels", help="페이지에서 복사한 JSON 을 jsonl 에 반입한다")
    imp.add_argument("source", help="라벨 JSON 파일 경로")
    imp.add_argument("--labels", help=f"jsonl 경로(기본 {DEFAULT_LABELS})")
    imp.set_defaults(func=_cmd_import)

    report = sub.add_parser("report", help="표시 결과를 집계한다")
    report.add_argument("--labels", help=f"jsonl 경로(기본 {DEFAULT_LABELS})")
    report.add_argument("--json", action="store_true", help="JSON 으로 출력")
    report.add_argument("--records-dir", help=argparse.SUPPRESS)
    report.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args, out or sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
