"""워크플로 배선이 조용히 빠지지 않게 고정한다.

이 파일이 지키는 것은 "코드가 맞게 도는가"가 아니라 "CI 가 그 코드를 부르는가"다.
실측으로, 발송 전 점검 스크립트(`scripts/preflight.py`)는 만들어 두고도 어떤
워크플로에도 연결돼 있지 않아 반년 가까이 수동 전용이었다(2026-09-16 확인).
같은 일이 다시 일어나면 테스트가 먼저 알려준다.
"""

from __future__ import annotations

import os
import subprocess
import sys
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


def test_preflight_passes_on_the_current_code():
    """preflight 는 pytest 밖에 있어 계약을 바꾸고도 CI 에서야 깨진 것을 알았다.

    실측 2026-11-17~18: §59 에서 사람 지시를 공용 프롬프트에서 뺐는데 preflight 는
    옛 계약(사람 지시가 분석·상대 평가 프롬프트에 들어간다)을 검사하고 있어 이틀 연속
    발송 전에 멈췄다. 같은 일이 다시 나면 push 전에 여기서 걸린다.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SUPABASE", "SMTP", "RESEND"))}
    result = subprocess.run(
        [sys.executable, "scripts/preflight.py"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    tail = "\n".join(result.stdout.splitlines()[-5:])
    assert result.returncode == 0, f"preflight 실패\n{tail}\n{result.stderr[-1500:]}"


def test_failure_alerts_go_to_the_channels_we_read():
    """알림이 아무도 보지 않는 채널로 가면 실패는 조용해진다(PROGRESS §44)."""
    for workflow, job in (("daily_digest.yml", "digest"), ("evals.yml", "evals")):
        notify = [s for s in _steps(workflow, job) if "evals.alert" in (s.get("run") or "")]
        assert notify, f"{workflow}: 알림 단계가 없다"
        env = notify[0].get("env") or {}
        assert "DISCORD_BOT_TOKEN" in env and "RESEND_API_KEY" in env, f"{workflow}: {sorted(env)}"


def test_candidate_pool_is_committed_with_the_digest():
    """후보 풀이 커밋되지 않으면 러너와 함께 사라져 사용자별 재선정을 할 수 없다(PROGRESS §55)."""
    publish = [
        s.get("run") or ""
        for s in _steps("daily_digest.yml", "digest")
        if "git add docs/" in (s.get("run") or "")
    ]
    assert publish, "커밋 단계가 없다"
    assert "git add data/pool/" in publish[0]
    assert "!data/pool/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def _env_of(workflow: str, job: str, marker: str) -> dict:
    steps = [s for s in _steps(workflow, job) if marker in (s.get("run") or "")]
    assert steps, f"{workflow}: '{marker}' 단계가 없다"
    return steps[0].get("env") or {}


SMTP_ENV = {"EMAIL_PROVIDER", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"}


def test_subscriber_settings_reach_the_digest_run():
    """구독자 발송은 service role 키와 발송 경로 설정이 러너에 내려와야 켜진다(PROGRESS §56)."""
    env = _env_of("daily_digest.yml", "digest", "app.jobs.daily_digest")
    assert {"SUPABASE_SERVICE_KEY", *SMTP_ENV} <= set(env), sorted(env)
    # 폴백 문자열을 워크플로에 두지 않는다. 빈 값은 config 가 기본값으로 되돌린다.
    for name in ("EMAIL_PROVIDER", "SMTP_PORT"):
        assert "||" not in env[name], f"{name}: 워크플로와 config 의 기본값 이중화"


def test_resend_job_gets_the_mail_route_but_not_supabase():
    env = _env_of("resend_email.yml", "resend", "app.jobs.resend_email")
    assert SMTP_ENV <= set(env), sorted(env)
    assert not [name for name in env if name.startswith("SUPABASE")], "재발송은 구독자에게 가지 않는다"


def test_failure_alerts_use_the_operator_address():
    """운영 알림은 ALERT_EMAIL_TO 로 간다(없으면 EMAIL_TO). 발송 경로 설정도 함께 내려온다."""
    for workflow, job in (("daily_digest.yml", "digest"), ("evals.yml", "evals")):
        env = _env_of(workflow, job, "evals.alert")
        assert {"ALERT_EMAIL_TO", "EMAIL_TO", *SMTP_ENV} <= set(env), f"{workflow}: {sorted(env)}"


def test_env_example_lists_the_new_settings():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("SUPABASE_SERVICE_KEY", "ALERT_EMAIL_TO", *SMTP_ENV):
        assert f"\n{name}=" in text, name


def test_effect_report_is_report_only_in_weekly_evals():
    """개인화 효과 보고서는 게이트를 깨지 않고, 이름과 파일을 남기지 않는다(PROGRESS §57)."""
    steps = [s for s in _steps("evals.yml", "evals") if "evals.effect" in (s.get("run") or "")]
    assert steps, "효과 보고서 단계가 없다"
    step = steps[0]
    assert step.get("continue-on-error") is True and step.get("if") == "always()"
    assert "--anonymize" in step["run"], "공개 로그에 구독자별 표가 찍힌다"
    assert "--out" not in step["run"] and "git add" not in step["run"]
    env = step.get("env") or {}
    assert {"SUPABASE_URL", "SUPABASE_SERVICE_KEY"} <= set(env), sorted(env)
    # 메일·채널 시크릿은 내려보내지 않는다. 이 단계는 읽기만 한다.
    assert not [n for n in env if not n.startswith("SUPABASE")], sorted(env)


def test_env_example_lists_the_effect_settings():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("EFFECT_HOLDOUT_SLOTS", "EFFECT_HOLDOUT_RATE", "TRACK_CLICKS"):
        assert f"\n{name}=" in text, name
