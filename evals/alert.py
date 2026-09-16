"""품질 게이트 실패 알림을 실제로 읽는 채널로 보낸다.

왜 별도 모듈인가:
    evals/notify.py 는 리포트를 사람이 읽는 문구로 만드는 일만 한다. 어디로
    보내는지는 바뀌어도 문구는 그대로여야 해서 갈라 둔다.

왜 Discord 와 이메일인가:
    2026-09-01 발송이 Telegram 에서 Discord 로 옮겨간 뒤에도 이 알림만 Telegram
    으로 갔다. 그래서 8주 동안 게이트가 실패하는 사이 사용자에게 남은 것은
    GitHub 기본 실패 메일("워크플로가 실패했다")뿐이었고, 무엇이 실패했는지는
    아무데도 닿지 않았다. 정본 채널(Discord)과 이메일 둘 다로 보낸다.

왜 실패를 삼키지 않는가:
    이전 단계는 `curl ... || true` 로 끝나 전송이 실패해도 늘 성공으로 보였다.
    알림이 안 왔는지, 보냈는데 안 왔는지 구분할 수 없었다. 여기서는 채널별
    결과를 stdout 에 적고, 하나라도 실패하면 종료코드 1 을 낸다(워크플로는
    이미 실패 상태이므로 게이트 판정에는 영향이 없다).

쓰는 곳:
    - 주간 품질 게이트(evals.yml): 리포트 파일을 넘겨 위반·회귀 내용을 보낸다.
    - 다이제스트(daily_digest.yml): 파이썬 진입 전 실패라 리포트가 없다. --text 로
      문구만 보낸다. 이 모듈은 표준 라이브러리만 쓰므로 의존성 설치가 실패한 뒤에도
      동작한다(파이썬 설치 단계까지는 성공했어야 한다).

사용:
    python -m evals.alert evals_report.json --run-url <url>
    python -m evals.alert --text "다이제스트 워크플로 실패" --subject "[DS Digest] ..." --run-url <url>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from evals.notify import format_report

DISCORD_API = "https://discord.com/api/v10"
RESEND_API = "https://api.resend.com/emails"
# Discord 는 2000자를 넘기면 400 을 낸다. 보내기 전에 자른다.
MAX_DISCORD_CONTENT = 2000
TIMEOUT = 15
DEFAULT_SUBJECT = "[DS Digest] 주간 품질 게이트 실패"
# urllib 기본 UA(python-urllib/3.12)는 Cloudflare 앞단에서 막힌다. 2026-09-16
# 러너 실측에서 Discord API 와 Resend API 가 둘 다 `403 error code: 1010`(브라우저
# 시그니처 기반 차단)을 돌려줬다. 같은 날 피드 프로브에서도 헤더 없는 요청만
# 같은 응답을 받았다. 알림이 나가지 않는 것은 게이트가 실패한 것보다 조용해서
# 더 나쁘다.
USER_AGENT = "ds-digest-alert/1.0 (+https://github.com/sangho24/ds-digest)"


def build_text(report: dict[str, Any], run_url: str) -> str:
    """알림 본문. 채널이 달라도 같은 내용을 보낸다."""
    return f"📉 주간 품질 게이트 실패\n\n{format_report(report)}\n\n{run_url}"


def build_message(message: str, run_url: str) -> str:
    """리포트가 없는 실패(파이썬 진입 전 등)의 본문."""
    return f"⚠️ {message}\n\n{run_url}"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:  # 망 오류, 타임아웃
        return 0, f"{type(e).__name__}: {e}"


def send_discord(text: str, env: dict[str, str]) -> tuple[str, bool, str]:
    token, channel = env.get("DISCORD_BOT_TOKEN"), env.get("DISCORD_CHANNEL_ID")
    if not token or not channel:
        return "discord", True, "설정 없음, 건너뜀"
    status, detail = _post(
        f"{DISCORD_API}/channels/{channel}/messages",
        {"content": _truncate(text, MAX_DISCORD_CONTENT)},
        {"Authorization": f"Bot {token}"},
    )
    return "discord", 200 <= status < 300, f"status={status} {detail}".strip()


def send_email(text: str, env: dict[str, str], subject: str = DEFAULT_SUBJECT) -> tuple[str, bool, str]:
    key, sender, to = env.get("RESEND_API_KEY"), env.get("EMAIL_FROM"), env.get("EMAIL_TO")
    if not key or not sender or not to:
        return "email", True, "설정 없음, 건너뜀"
    status, detail = _post(
        RESEND_API,
        {"from": sender, "to": [t.strip() for t in to.split(",") if t.strip()],
         "subject": subject, "text": text},
        {"Authorization": f"Bearer {key}"},
    )
    return "email", 200 <= status < 300, f"status={status} {detail}".strip()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?", help="evals/run.py --json 결과 파일")
    ap.add_argument("--text", help="리포트 대신 보낼 문구. 리포트가 없는 실패에 쓴다")
    ap.add_argument("--subject", default=DEFAULT_SUBJECT, help="이메일 제목")
    ap.add_argument("--run-url", default="", help="실패한 워크플로 실행 URL")
    ap.add_argument("--dry-run", action="store_true", help="보내지 않고 본문만 출력")
    args = ap.parse_args(argv[1:])

    if args.text:
        text = build_message(args.text, args.run_url)
    else:
        if not args.report:
            ap.error("report 파일이나 --text 중 하나는 있어야 한다")
        try:
            report = json.loads(open(args.report, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError) as error:
            # 리포트를 못 읽어도 알림 자체는 나가야 한다. 게이트는 이미 실패했다.
            report = {}
            print(f"리포트를 읽지 못했습니다: {error}")
        text = build_text(report, args.run_url)
    if args.dry_run:
        print(text)
        return 0

    env = dict(os.environ)
    results = [send_discord(text, env), send_email(text, env, args.subject)]
    for channel, ok, detail in results:
        print(f"{channel}: {'ok' if ok else '실패'} {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
