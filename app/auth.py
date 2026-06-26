"""
auth.py — 인증 모듈.

로그인(POST /api/auth/login) → JWT 발급.
모든 관리 API는 JWT 미들웨어를 거친다.

설정 (.env 또는 환경변수):
  IOTRADIO_ADMIN_USER   관리자 아이디 (기본 admin)
  IOTRADIO_ADMIN_HASH   bcrypt 해시. 미설정 시 서버 기동 거부.
  IOTRADIO_JWT_SECRET   JWT 서명 키. 미설정 시 서버 기동 거부.
  IOTRADIO_JWT_EXPIRE_H JWT 만료 시간(시간 단위, 기본 12)

비밀번호 해시 생성:
  python3 -c "import bcrypt; print(bcrypt.hashpw(b'비밀번호', bcrypt.gensalt()).decode())"
"""

import os
import time
import functools

import bcrypt
import jwt
from aiohttp import web

from logging_conf import get_logger

log = get_logger("auth")

# ── 설정 로드 ───────────────────────────────────────────────

def _require_env(key):
    v = os.environ.get(key, "").strip()
    if not v:
        raise RuntimeError(
            f"[AUTH] {key} is not set. "
            f"Set it in .env before starting the server.")
    return v

def _load_config():
    """기동 시 1회 호출. 필수 환경변수 누락 시 즉시 중단."""
    return {
        "user": os.environ.get("IOTRADIO_ADMIN_USER", "admin"),
        "hash": _require_env("IOTRADIO_ADMIN_HASH"),
        "secret": _require_env("IOTRADIO_JWT_SECRET"),
        "expire_h": int(os.environ.get("IOTRADIO_JWT_EXPIRE_H", "12")),
    }

_cfg = None

def init_auth():
    """server.py 기동 시 호출해 설정을 검증한다."""
    global _cfg
    _cfg = _load_config()
    log.info("[AUTH] initialized user=%s expire=%dh",
             _cfg["user"], _cfg["expire_h"])


# ── JWT 유틸 ────────────────────────────────────────────────

def _issue_token():
    now = int(time.time())
    payload = {
        "sub": _cfg["user"],
        "iat": now,
        "exp": now + _cfg["expire_h"] * 3600,
    }
    return jwt.encode(payload, _cfg["secret"], algorithm="HS256")


def _verify_token(token: str):
    """유효하면 payload 반환. 만료·위조 시 예외."""
    return jwt.decode(token, _cfg["secret"], algorithms=["HS256"])


# ── 로그인 핸들러 ───────────────────────────────────────────

async def login_handler(request):
    """
    POST /api/auth/login
    Body JSON: {username, password}
    Response:  {ok, token, expires_in}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "invalid JSON"}, status=400)

    username = body.get("username", "")
    password = body.get("password", "")

    # 아이디 + 비밀번호 검증 (타이밍 어택 방지를 위해 항상 bcrypt 실행)
    user_ok = username == _cfg["user"]
    pw_bytes = password.encode() if password else b""
    hash_bytes = _cfg["hash"].encode()
    pw_ok = bcrypt.checkpw(pw_bytes, hash_bytes)

    if not (user_ok and pw_ok):
        log.warning("[AUTH] login failed user=%s from=%s", username, request.remote)
        return web.json_response(
            {"ok": False, "error": "invalid credentials"}, status=401)

    token = _issue_token()
    log.info("[AUTH] login ok user=%s from=%s", username, request.remote)
    return web.json_response({
        "ok": True,
        "token": token,
        "expires_in": _cfg["expire_h"] * 3600,
    })


# ── JWT 미들웨어 ────────────────────────────────────────────

# 인증 없이 통과하는 경로
_PUBLIC_PATHS = {
    "/api/auth/login",   # 로그인
    "/",                 # 웹 UI index
}
_PUBLIC_PREFIXES = (
    "/files/",           # nginx 정적 서빙(단말 다운로드)
)

def _is_public(path):
    if path in _PUBLIC_PATHS:
        return True
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    # 웹 정적 파일 (.js .css .ico 등)
    if "." in path.split("/")[-1]:
        return True
    return False


@web.middleware
async def jwt_middleware(request, handler):
    """
    WSS 업그레이드 요청은 /cmd, /audio는 디바이스 채널(별도 인증 예정)이므로
    미들웨어를 통과시킨다. /ingest는 자체 Bearer 토큰 검증이 있다.
    HTTP 관리 API는 JWT가 없으면 401.
    """
    path = request.path

    # WebSocket 업그레이드: 채널 자체 인증에 위임
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await handler(request)

    if _is_public(path):
        return await handler(request)

    # JWT 검증
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return web.json_response(
            {"ok": False, "error": "missing token"}, status=401)
    token = auth[7:]
    try:
        _verify_token(token)
    except jwt.ExpiredSignatureError:
        return web.json_response(
            {"ok": False, "error": "token expired"}, status=401)
    except jwt.InvalidTokenError:
        return web.json_response(
            {"ok": False, "error": "invalid token"}, status=401)

    return await handler(request)