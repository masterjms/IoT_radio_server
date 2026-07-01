"""
server.py — 진입점.

aiohttp 앱을 구성하고 라우트를 등록한 뒤 기동한다.
nginx가 TLS를 종단하고 이 평문 포트(기본 8080)로 프록시한다.

라우트 구성:
  WSS:
    /cmd      제어 채널          [이번 단계 구현]
    /audio    오디오 팬아웃      [3단계 예정]
    /ingest   군포 앱 오디오 수신 [4단계 예정]
  HTTP:
    /upload   파일 업로드        [2단계 예정]
    /broadcast 방송 트리거        [2단계 예정]
    /api/health 상태 조회         [이번 단계 구현]

정적 파일(/files/)은 nginx가 직접 서빙하므로 여기서 다루지 않는다.
"""

import json

from aiohttp import web

import config
from logging_conf import setup_logging, get_logger
from registry import Registry
from session import SessionManager
from file_manager import FileManager
import cmd_channel
import audio_channel
import ingest_channel
import http_api
from auth import jwt_middleware, login_handler, init_auth

log = get_logger("server")


# ── HTTP: 상태 조회 ────────────────────────────────────────

async def health_handler(request):
    """GET /api/health — 서버·연결·상태 스냅샷."""
    registry = request.app["registry"]
    session = request.app["session"]
    runtime = request.app.get("live")
    body = {
        "ok": True,
        "session": session.snapshot(),
        "cmd_devices": len(registry.all_cmd()),
        "audio_devices": registry.audio_count(),
        "devices": registry.device_list(),
        "ingest_connected": registry.has_ingest(),
        "live_stats": runtime.stats() if runtime else None,
    }
    return web.json_response(body)


# ── 아직 구현되지 않은 라우트 (자리표시) ───────────────────

async def not_yet(request):
    """다음 단계에서 구현될 엔드포인트. 라우팅/배선 확인용."""
    return web.json_response(
        {"ok": False, "error": "not_implemented",
         "path": request.path},
        status=501,
    )


# ── 앱 구성 ────────────────────────────────────────────────

def make_app():
    config.ensure_dirs()
    init_auth()   # 필수 환경변수 검증 — 누락 시 여기서 기동 중단
    app = web.Application(
        client_max_size=config.FILE_MAX_BYTES + 1024 * 1024,
        middlewares=[jwt_middleware],
    )

    # 공유 상태를 앱 컨텍스트에 둔다.
    app["registry"] = Registry()
    app["session"] = SessionManager()
    app["file_manager"] = FileManager()
    app["live"] = None        # 현재 LiveRuntime (없으면 None)

    # WSS
    app.router.add_get("/cmd", cmd_channel.cmd_handler)      # 구현됨
    app.router.add_get("/audio", audio_channel.audio_handler)  # 구현됨
    app.router.add_get("/ingest", ingest_channel.ingest_handler)  # 구현됨
    # HTTP
    app.router.add_post("/upload", http_api.upload_handler)
    app.router.add_post("/broadcast", http_api.broadcast_handler)
    app.router.add_post("/api/auth/login", login_handler)
    app.router.add_post("/api/live/start", http_api.live_start_handler)
    app.router.add_post("/api/live/stop", http_api.live_stop_handler)
    app.router.add_get("/api/files", http_api.files_handler)
    app.router.add_delete("/api/files/{file_name}", http_api.file_delete_handler)
    app.router.add_get("/api/health", health_handler)

    return app


def main():
    setup_logging()
    app = make_app()
    log.info("[BOOT] iotradio server starting on %s:%d (domain=%s)",
             config.HOST, config.PORT, config.PUBLIC_DOMAIN)
    web.run_app(app, host=config.HOST, port=config.PORT, print=None)


if __name__ == "__main__":
    main()