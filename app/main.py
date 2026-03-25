"""
FastAPI 메인 앱
- GET /api/feedback : 이메일 링크 클릭 시 피드백 수신
- POST /api/feedback : 프로그래밍 방식 피드백
- POST /api/trigger : 수동으로 다이제스트 트리거 (백그라운드 실행)
- 앱 시작 시 Telegram 콜백 폴링 루프 백그라운드 실행
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse

from app.models import FeedbackPayload
from app.feedback import process_feedback


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱 시작: Telegram 폴링 백그라운드 태스크 시작
    from app.deliverers.polling import start_polling
    polling_task = asyncio.create_task(start_polling())
    yield
    # 앱 종료: 폴링 태스크 취소
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="DS Digest", version="0.1.0", lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": "DS Digest", "status": "running"}


@app.get("/api/feedback")
async def feedback_via_link(
    action: str = Query(...),
    item_url: str = Query(""),
    keyword: str = Query(""),
    user_id: str = Query("default"),
):
    """이메일의 👍/👎 링크 클릭 시 호출"""
    payload = FeedbackPayload(
        user_id=user_id,
        item_url=item_url,
        action=action,
        keyword=keyword if keyword else None,
    )

    if action == "keyword_request" and not keyword:
        # 키워드 입력 폼 보여주기
        return HTMLResponse(_keyword_form_html(user_id))

    process_feedback(payload)
    return HTMLResponse(_thank_you_html(action))


@app.post("/api/feedback")
async def feedback_via_api(payload: FeedbackPayload):
    """프로그래밍 방식 피드백"""
    profile = process_feedback(payload)
    return {"status": "ok", "profile_topics": profile.preferred_topics}


@app.post("/api/trigger")
async def trigger_digest(background_tasks: BackgroundTasks):
    """
    수동 다이제스트 트리거 (개발/테스트용).
    BackgroundTasks를 사용해 즉시 200 응답 후 백그라운드에서 파이프라인 실행.
    """
    from app.jobs.daily_digest import run_daily_digest

    async def _run():
        await run_daily_digest()

    background_tasks.add_task(_run)
    return {"status": "triggered", "message": "백그라운드에서 파이프라인이 시작됐습니다."}


# ──────────────────────────────────────────────
# Response HTML (간단한 인라인)
# ──────────────────────────────────────────────

def _thank_you_html(action: str) -> str:
    messages = {
        "like": "👍 피드백 감사합니다! 비슷한 콘텐츠를 더 보내드릴게요.",
        "dislike": "👎 알겠습니다. 다음엔 더 좋은 콘텐츠를 찾아볼게요.",
        "keyword_request": "✅ 키워드가 등록되었습니다! 다음 다이제스트에 반영됩니다.",
        "unsubscribe": "😔 수신 거부 처리됐어요. 언제든 다시 구독할 수 있어요.",
    }
    msg = messages.get(action, "피드백이 접수되었습니다.")
    return f"""
    <html><body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;background:#f5f5f0;">
    <div style="text-align:center;padding:40px;background:#fff;border-radius:16px;max-width:400px;">
        <p style="font-size:18px;margin-bottom:8px;">{msg}</p>
        <p style="font-size:13px;color:#999;">이 탭을 닫아도 됩니다.</p>
    </div></body></html>
    """


def _keyword_form_html(user_id: str) -> str:
    return f"""
    <html><body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;background:#f5f5f0;">
    <div style="text-align:center;padding:40px;background:#fff;border-radius:16px;max-width:400px;">
        <p style="font-size:18px;margin-bottom:16px;">다음에 받고 싶은 주제를 알려주세요</p>
        <input id="kw" type="text" placeholder="예: feature store, causal inference"
               style="width:100%;padding:10px;border:1px solid #ddd;border-radius:8px;font-size:14px;box-sizing:border-box;margin-bottom:12px;">
        <button onclick="submitKw()" style="width:100%;padding:10px;background:#1a1a1a;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;">
            등록하기
        </button>
        <script>
        function submitKw() {{
            const kw = document.getElementById('kw').value.trim();
            if (kw) window.location = '/api/feedback?action=keyword_request&keyword=' + encodeURIComponent(kw) + '&user_id={user_id}';
        }}
        document.getElementById('kw').addEventListener('keydown', e => {{ if(e.key==='Enter') submitKw(); }});
        </script>
    </div></body></html>
    """
