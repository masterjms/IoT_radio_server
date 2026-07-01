"""
config.py — 서버 전역 설정.

운영 환경에서는 민감값(인제스트 토큰 등)을 .env 또는 환경변수로 주입한다.
여기서는 기본값을 정의하고, 환경변수가 있으면 그것을 우선한다.
"""

import os


# ── 네트워크 ────────────────────────────────────────────────
# nginx가 TLS를 종단하고 이 평문 포트로 프록시한다.
HOST = os.environ.get("IOTRADIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("IOTRADIO_PORT", "8080"))

# 외부에 노출되는 도메인. FILE_START.https_url 생성에 쓰인다.
PUBLIC_DOMAIN = os.environ.get("IOTRADIO_DOMAIN", "iotradio.co.kr")


# ── 경로 ────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(BASE_DIR, "media")
UPLOAD_DIR = os.path.join(MEDIA_DIR, "uploads")
SAMPLE_DIR = os.path.join(MEDIA_DIR, "samples")

# nginx가 /files/ 로 정적 서빙하는 경로와 매칭된다.
FILES_URL_PREFIX = "/files"


# ── 프로토콜 상수 (사양 06 문서 기준) ───────────────────────
PROTO_VER = 267          # 현재 시험 기준 ver

# LIVE 기본/보정값
DEFAULT_FRAME_MS = 40
DEFAULT_SAMPLE_RATE = 16000
READY_TIMEOUT_MIN = 1
READY_TIMEOUT_MAX = 60
READY_TIMEOUT_DEFAULT = 30

# Opus payload 권장 상한 (SDIO 4096B - 헤더 여유)
OPUS_PAYLOAD_MAX = 4000

# 파일 정책 (사양 06/07 기준)
FILE_MAX_BYTES = 4 * 1024 * 1024     # P4 현재 제약: 최대 4MB


# ── 인증 ────────────────────────────────────────────────────
# 군포 앱이 /ingest 연결 시 제시하는 토큰. 신규 설계 항목.
INGEST_TOKEN = os.environ.get("IOTRADIO_INGEST_TOKEN", "change-me-in-env")


# ── heartbeat ───────────────────────────────────────────────
# 서버 주도 WebSocket 제어프레임 ping/pong. 단말이 텍스트로 보내는
# 자체 하트비트("ping" 문자열, cmd_channel에서 별도로 pong 응답)와는
# 완전히 다른 메커니즘이라 서로 간섭하지 않는다.
#
# 이걸 켜두지 않으면, 단말이 전원 차단이나 네트워크 단절처럼 TCP
# 종료 신호 없이 갑자기 사라졌을 때 서버가 이를 감지할 방법이 없어
# 죽은 연결이 레지스트리에 영원히 남는다(UI에 유령 단말로 표시됨).
WS_HEARTBEAT_SEC = 30         # 30초마다 ping, 응답 없으면 연결 종료
WS_HEARTBEAT_TIMEOUT = 10     # pong 미응답 허용시간


def ensure_dirs():
    """필요한 미디어 디렉토리를 생성한다."""
    for d in (MEDIA_DIR, UPLOAD_DIR, SAMPLE_DIR):
        os.makedirs(d, exist_ok=True)