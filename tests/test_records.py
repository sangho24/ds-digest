"""구조화 정본 저장(app/records.py) + Supabase best-effort 미러(app/db.py) 테스트.

- save_digest_records: tmp 디렉토리에 쓰고 다시 읽어 DigestRecord로 라운드트립.
  (base_dir 인자로 루트를 주입해 리포 data/records 오염을 막는다.)
- save_digest_records_to_db: dry_run일 때 no-op, Supabase 예외 시 raise 안 함.

실행: pytest tests/test_records.py -v
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import app.db as db
from app.models import (
    ContentAnalysis,
    DigestItem,
    DigestRecord,
    RawContent,
    SourceType,
)
from app.records import save_digest_records


def _digest_item(url: str = "https://example.com/a") -> DigestItem:
    raw = RawContent(
        source_type=SourceType.RSS,
        source_name="테스트 블로그",
        title="테스트 제목",
        url=url,
    )
    analysis = ContentAnalysis(
        relevance_score=6,
        one_line_summary="한 줄 요약",
        domain=["ai-ml"],
        content_type="tutorial",
    )
    return DigestItem(raw=raw, analysis=analysis)


# ──────────────────────────────────────────────
# save_digest_records — 로컬 JSON 정본 라운드트립
# ──────────────────────────────────────────────

def test_save_digest_records_roundtrip(tmp_path):
    items = [_digest_item("https://example.com/a"), _digest_item("https://example.com/b")]
    path = save_digest_records(items, "2026-07-21", base_dir=tmp_path)

    # 파일이 기대 경로에 생성됨
    assert path == tmp_path / "data" / "records" / "digest_2026-07-21.json"
    assert path.exists()

    # JSON이 유효하고 DigestRecord로 라운드트립됨
    data = json.loads(path.read_text(encoding="utf-8"))
    record = DigestRecord.model_validate(data)
    assert record.date == "2026-07-21"
    assert record.schema_version == 2
    assert record.generated_at  # ISO 문자열이 채워짐
    assert len(record.items) == 2  # 항목 수 일치
    assert record.items[0].raw.url == "https://example.com/a"
    assert record.items[0].analysis.relevance_score == 6


def test_save_digest_records_creates_dir(tmp_path):
    """data/records 디렉토리가 없어도 생성한다."""
    nested = tmp_path / "does-not-exist-yet"
    path = save_digest_records([_digest_item()], "2026-01-01", base_dir=nested)
    assert path.exists()
    assert (nested / "data" / "records").is_dir()


# ──────────────────────────────────────────────
# save_digest_records_to_db — dry_run no-op / 예외 삼킴
# ──────────────────────────────────────────────

# 주의: save_digest_records_to_db는 db 모듈 상단에서 `from app.config import
# get_settings`로 이름을 import-time 바인딩한다. 따라서 반드시 `db.get_settings`를
# 패치해야 한다("app.config.get_settings" 패치는 db의 참조에 먹지 않아 무의미).
# 또한 가드 위반은 spy가 raise하면 SUT의 blanket except에 삼켜지므로,
# 호출 여부를 플래그로 기록해 함수 반환 "후"에 assert한다.

def test_save_to_db_noop_when_dry_run(monkeypatch):
    """dry_run이면 Supabase를 아예 건드리지 않는다."""
    called = {"supabase": False}

    def _spy_supabase():
        called["supabase"] = True
        raise RuntimeError("dry_run에서 호출되면 안 됨")  # 삼켜지지만 called로 감지

    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(dry_run=True))
    monkeypatch.setattr(db, "get_supabase", _spy_supabase)

    # 예외 없이 즉시 반환되어야 한다
    asyncio.run(db.save_digest_records_to_db([_digest_item()], "2026-07-21"))

    # 반환 후 검증 — SUT의 except에 삼켜지지 않는 지점에서 단언
    assert called["supabase"] is False, "dry_run에서 Supabase를 호출하면 안 된다"


def test_save_to_db_swallows_exceptions(monkeypatch):
    """Supabase 연결/쿼리 실패 등 모든 예외를 삼키고 raise하지 않는다(best-effort)."""
    def _boom():
        raise RuntimeError("network down / table missing")

    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(dry_run=False))
    monkeypatch.setattr(db, "get_supabase", _boom)

    # dry_run=False라 실제로 get_supabase 경로까지 도달 → 예외가 삼켜져 정상 반환되면 통과
    asyncio.run(db.save_digest_records_to_db([_digest_item()], "2026-07-21"))


def test_save_to_db_empty_items_noop(monkeypatch):
    """빈 리스트면 Supabase 호출 없이 반환한다."""
    called = {"supabase": False}

    def _spy_supabase():
        called["supabase"] = True
        raise RuntimeError("빈 리스트에서 호출되면 안 됨")

    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(dry_run=False))
    monkeypatch.setattr(db, "get_supabase", _spy_supabase)

    asyncio.run(db.save_digest_records_to_db([], "2026-07-21"))

    assert called["supabase"] is False, "빈 리스트에서 Supabase를 호출하면 안 된다"


# ──────────────────────────────────────────────
# 드라이런 출력 격리 (실제 사고 회귀)
# ──────────────────────────────────────────────
#
# 드라이런이 data/records/digest_{오늘}.json과 docs/{오늘}.html을 mock 데이터로
# 만들었는데, 그날 산출물이 아직 커밋 전이라 두 파일이 untracked였다.
# `git checkout -- data/ docs/`는 tracked 파일만 되돌리므로 mock이 살아남았고,
# 이어진 `git add -A`가 그걸 커밋해 GitHub Pages에 "[DRY RUN]" 다이제스트가
# 공개됐다. 되돌리는 절차를 조심하는 것으로는 부족하다 — 애초에 안 써야 한다.

import asyncio  # noqa: E402

import app.jobs.daily_digest as job  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import RawContent, SourceType  # noqa: E402


def test_dry_run_leaves_real_output_paths_untouched(monkeypatch):
    """드라이런은 data/records/ 와 docs/ 를 건드리면 안 된다.

    경로 결정 로직을 재구현하지 않고 실제 run_daily_digest를 태운다 — 사고가
    난 곳이 바로 그 실제 경로 결정이었기 때문이다.
    """
    root = Path(job.__file__).parent.parent.parent
    records_dir, docs_dir = root / "data" / "records", root / "docs"

    def snapshot(d):
        return {p.name: p.stat().st_mtime_ns for p in d.iterdir() if p.is_file()}

    before_records, before_docs = snapshot(records_dir), snapshot(docs_dir)

    async def _fake_collect(*a, **kw):
        item = RawContent(
            source_type=SourceType.RSS, source_name="src", source_key="src",
            title="드라이런 테스트", url="https://example.com/dryrun-test",
            body="본문 " * 300,
        )
        return [], [item]

    async def _none(*a, **kw):
        return []

    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(job, "collect_all", _fake_collect)
    # 나머지 수집기도 전부 막는다. 안 막으면 arXiv 카테고리 10개 × HTTP 호출이
    # 테스트마다 실제로 나가서 스위트가 수십 초씩 느려진다(실측 20초 → 5분).
    import app.collectors as collectors
    import app.collectors_newsletter as newsletter
    monkeypatch.setattr(collectors, "fetch_arxiv_recent", _none)
    monkeypatch.setattr(collectors, "fetch_hackernews_recent", _none)
    monkeypatch.setattr(newsletter, "fetch_newsletters_recent", _none)

    asyncio.run(job.run_daily_digest())
    get_settings.cache_clear()

    assert snapshot(records_dir) == before_records, "드라이런이 data/records/를 변경했다"
    assert snapshot(docs_dir) == before_docs, "드라이런이 docs/를 변경했다"
    assert (root / "data" / "dryrun").exists()


def test_save_digest_records_honors_base_dir(tmp_path):
    """base_dir override가 실제로 그 아래에 쓴다 — 드라이런 격리가 여기에 기댄다."""
    from app.records import save_digest_records

    path = save_digest_records([], "2026-09-01", base_dir=tmp_path)

    assert tmp_path in path.parents
    assert path.name == "digest_2026-09-01.json"
