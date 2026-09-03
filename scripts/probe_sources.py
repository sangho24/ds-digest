"""릴리스 추적 소스의 접근 가능 여부를 실측한다.

W0 성립 판정의 첫 단계. data/watchlist.yaml 의 `verify` 대상(HF org 이름,
공식 블로그 피드 URL)이 실제로 존재하는지 확인하고, 조직별 최근 릴리스
건수를 세어 "릴리스 밀도가 알림을 지탱하는가"를 판단할 근거를 만든다.

로컬 실행 시 huggingface.co 가 망에서 차단될 수 있다(2026-09-03 실측: SNI 차단).
그 경우 HF 항목은 blocked 로 기록되고 나머지는 정상 진행한다.
GitHub Actions 러너는 다른 망이므로 workflow_dispatch 로 돌리면 된다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "data" / "watchlist.yaml"
UA = "ds-digest-probe/1.0 (+https://github.com/sangho24/ds-digest)"
TIMEOUT = 15


@dataclass
class ProbeResult:
  target: str          # 조직 key
  kind: str            # "hf" | "blog"
  url: str
  status: str          # ok | not_found | blocked | error | skipped
  detail: str = ""
  count: int = 0       # hf: 최근 창 안의 신규 리포 수 (진짜 릴리스에 가깝다)
  touched: int = 0     # hf: 최근 창 안에 수정된 리포 수 (상한값)


def fetch(url: str) -> tuple[str, Any, str]:
  """(status, payload, detail) 반환. 예외를 밖으로 던지지 않는다."""
  req = urllib.request.Request(url, headers={"User-Agent": UA})
  try:
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
      return "ok", r.read(), f"HTTP {r.status}"
  except urllib.error.HTTPError as e:
    # 404 는 "이름이 틀렸다"는 신호라 차단과 구분해야 한다
    return ("not_found" if e.code == 404 else "error"), None, f"HTTP {e.code}"
  except urllib.error.URLError as e:
    reason = str(e.reason)
    # TLS 핸드셰이크 리셋은 망 차단의 전형적 증상이다
    blocked = any(k in reason for k in ("reset by peer", "Errno 54", "ConnectionResetError"))
    return ("blocked" if blocked else "error"), None, reason[:120]
  except Exception as e:  # noqa: BLE001 - 프로브는 어떤 이유로도 죽으면 안 된다
    return "error", None, f"{type(e).__name__}: {e}"[:120]


def probe_hf(org: dict, days: int) -> ProbeResult:
  hf_org = org.get("hf_org")
  if not hf_org:
    return ProbeResult(org["key"], "hf", "", "skipped", "폐쇄형이라 HF org 없음")

  base = f"https://huggingface.co/api/models?author={hf_org}&limit=100&direction=-1"
  series = [x.lower() for x in org.get("series") or []]
  cutoff = datetime.now(timezone.utc) - timedelta(days=days)

  def parse(ts: str | None) -> datetime | None:
    if not ts:
      return None
    try:
      return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
      return None

  def matches(m: dict) -> bool:
    mid = (m.get("modelId") or "").split("/", 1)[-1].lower()
    return not series or any(mid.startswith(x) for x in series)

  def load(sort: str) -> tuple[str, list | None, str]:
    url = f"{base}&sort={sort}"
    status, body, detail = fetch(url)
    if status != "ok":
      return status, None, detail
    try:
      return "ok", json.loads(body), detail
    except json.JSONDecodeError as e:
      return "error", None, f"JSON 파싱 실패: {e}"

  # 두 번 조회한다. 한 정렬로는 둘 다 정확히 셀 수 없다.
  #   lastModified 정렬 = 최근 수정된 100건 -> touched 의 근거
  #   createdAt 정렬    = 최근 생성된 100건 -> created 의 근거
  # 한쪽만 쓰면 리포가 많은 org 에서 반대쪽이 과소 집계된다.
  st_mod, mods, detail = load("lastModified")
  if st_mod != "ok":
    return ProbeResult(org["key"], "hf", base, st_mod, detail)
  if not mods:
    return ProbeResult(org["key"], "hf", base, "not_found", "응답 200이나 모델 0건")

  time.sleep(0.3)
  st_new, news, _ = load("createdAt")
  created_reliable = st_new == "ok" and news is not None

  touched = sum(1 for m in mods
                if matches(m) and (t := parse(m.get("lastModified")) or parse(m.get("createdAt")))
                and t >= cutoff)

  pool = news if created_reliable else mods
  created = sum(1 for m in pool
                if matches(m) and (c := parse(m.get("createdAt"))) and c >= cutoff)

  # createdAt 정렬 상위 100건이 전부 창 안이면 100건에서 잘렸을 수 있다
  saturated = created_reliable and created >= 100
  note = "" if created_reliable else " [createdAt 정렬 실패, 하한값]"
  if saturated:
    note = " [100건 상한에 도달, 하한값]"

  return ProbeResult(
    org["key"], "hf", base, "ok",
    f"최근 {days}일 신규 {created}건 (수정 {touched}건){note}",
    created, touched)


def probe_blog(org: dict) -> ProbeResult:
  url = org.get("blog")
  if not url:
    return ProbeResult(org["key"], "blog", "", "skipped", "피드 URL 미등록")

  status, body, detail = fetch(url)
  if status != "ok":
    return ProbeResult(org["key"], "blog", url, status, detail)

  head = body[:600].lstrip().lower()
  if b"<rss" in head or b"<feed" in head or b"<?xml" in head:
    return ProbeResult(org["key"], "blog", url, "ok", "피드 형식 확인됨")
  # HTML 이 오면 RSS 가 아니라 웹페이지다. 피드 URL 을 다시 찾아야 한다
  return ProbeResult(org["key"], "blog", url, "not_found", "HTML 응답(피드 아님)")


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--days", type=int, default=90, help="최근 며칠을 셀지")
  ap.add_argument("--only", choices=["hf", "blog"], help="한쪽만 검사")
  ap.add_argument("--delay", type=float, default=0.5, help="요청 간 간격(초)")
  ap.add_argument("--out", type=Path, help="결과 JSON 저장 경로")
  args = ap.parse_args()

  wl = yaml.safe_load(WATCHLIST.read_text(encoding="utf-8"))
  orgs = wl["orgs"]
  results: list[ProbeResult] = []

  for org in orgs:
    if args.only != "blog":
      results.append(probe_hf(org, args.days))
      time.sleep(args.delay)
    if args.only != "hf":
      results.append(probe_blog(org))
      time.sleep(args.delay)

  # ---- 보고 ----
  print(f"\n워치리스트 조직 {len(orgs)}개 / 프로브 {len(results)}건 "
        f"(최근 {args.days}일 기준)\n")
  print(f"{'조직':<16}{'종류':<6}{'상태':<11}설명")
  print("-" * 92)
  for r in results:
    if r.status == "skipped":
      continue
    print(f"{r.target:<16}{r.kind:<6}{r.status:<11}{r.detail}")

  by_status: dict[str, int] = {}
  for r in results:
    by_status[r.status] = by_status.get(r.status, 0) + 1
  print("\n상태 집계:", dict(sorted(by_status.items())))

  hf_ok = [r for r in results if r.kind == "hf" and r.status == "ok"]
  total_new = sum(r.count for r in hf_ok)
  total_touched = sum(r.touched for r in hf_ok)
  print(f"\nHF 접근 성공 조직: {len(hf_ok)}개")
  print(f"최근 {args.days}일 신규 리포: {total_new}건  <- 판정 기준")
  print(f"최근 {args.days}일 수정 포함: {total_touched}건 (상한값. README 수정도 포함)")

  active = sorted(((r.count, r.target) for r in hf_ok if r.count), reverse=True)
  if active:
    print("\n신규 리포 상위:", ", ".join(f"{t} {c}" for c, t in active[:8]))
  dead = [r.target for r in hf_ok if not r.count]
  if dead:
    print(f"신규 0건 조직 {len(dead)}개: {', '.join(dead)}")

  # W0 판정 기준은 계획 문서 6절과 같다. 신규 리포 기준으로 본다
  if [r for r in results if r.kind == "hf" and r.status == "blocked"]:
    print("\n[판정 불가] HF 가 이 망에서 차단됐다. GitHub Actions 에서 다시 돌릴 것")
  elif total_new >= 20:
    print(f"\n[성립] 신규 리포 {total_new}건 >= 20건. W1 로 진행")
  else:
    print(f"\n[미달] 신규 리포 {total_new}건 < 20건. 계획 7절 중단 조건 검토")

  bad = [r for r in results if r.status == "not_found"]
  if bad:
    print(f"\nwatchlist.yaml 수정 필요 {len(bad)}건:")
    for r in bad:
      print(f"  - {r.target} ({r.kind}): {r.detail} | {r.url}")

  if args.out:
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
      json.dumps({"probed_at": datetime.now(timezone.utc).isoformat(),
                  "days": args.days,
                  "results": [asdict(r) for r in results]},
                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {args.out}")

  return 0


if __name__ == "__main__":
  sys.exit(main())
