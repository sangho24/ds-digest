"""워크플로 배선이 조용히 빠지지 않게 고정한다.

이 파일이 지키는 것은 "코드가 맞게 도는가"가 아니라 "CI 가 그 코드를 부르는가"다.
실측으로, 발송 전 점검 스크립트(`scripts/preflight.py`)는 만들어 두고도 어떤
워크플로에도 연결돼 있지 않아 반년 가까이 수동 전용이었다(2026-09-16 확인).
같은 일이 다시 일어나면 테스트가 먼저 알려준다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _steps(workflow: str, job: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return doc["jobs"][job]["steps"]


def test_preflight_runs_before_the_digest():
    """배선이 깨진 날은 다이제스트가 통째로 빈다. 발송 전에 걸러야 한다."""
    steps = _steps("daily_digest.yml", "digest")
    names = [s.get("name") or s.get("uses") or "" for s in steps]
    runs = [s.get("run") or "" for s in steps]

    preflight = [i for i, r in enumerate(runs) if "scripts/preflight.py" in r]
    digest = [i for i, n in enumerate(names) if n == "Run daily digest"]
    assert preflight, f"preflight 단계가 없다: {names}"
    assert digest, f"다이제스트 단계가 없다: {names}"
    assert preflight[0] < digest[0], f"preflight 가 발송 뒤에 있다: {names}"


def test_failure_alerts_go_to_the_channels_we_read():
    """알림이 아무도 보지 않는 채널로 가면 실패는 조용해진다(PROGRESS §44)."""
    for workflow, job in (("daily_digest.yml", "digest"), ("evals.yml", "evals")):
        notify = [s for s in _steps(workflow, job) if "evals.alert" in (s.get("run") or "")]
        assert notify, f"{workflow}: 알림 단계가 없다"
        env = notify[0].get("env") or {}
        assert "DISCORD_BOT_TOKEN" in env and "RESEND_API_KEY" in env, f"{workflow}: {sorted(env)}"
