"""
ingest_channel.py — /ingest WSS (군포 앱 → 서버 오디오 수신).

이 채널은 사양 문서 범위 밖의 신규 설계 영역이다(방송국→서버 구간).
군포 네이티브 앱이 FFmpeg으로 16kHz mono Opus 40ms로 인코딩한 프레임을
WSS binary로 보내면, 서버가 받아 3단계의 페이싱 엔진에 공급한다.

설계:
 - /ingest 연결 자체가 LIVE 시작 트리거다.
   연결 → LIVE_START broadcast → C6가 /audio 접속 → 페이싱 시작.
 - 소스 어댑터 IngestSource는 LiveRuntime이 그대로 쓰는 소스다.
   (3단계 LocalOpusSource와 동일한 frames() 인터페이스)
 - 실시간이므로 큐가 가득 차면 가장 오래된 프레임을 버린다(백프레셔 대신 drop).

인증:
 - Authorization: Bearer <INGEST_TOKEN> 헤더로 검증한다.
 - 토큰 불일치 시 WSS 업그레이드 전에 401로 거절한다.

프레임 방어 검증(사양 06):
 - Ogg/OpusHead/OpusTags로 시작하는 payload는 잘못된 것이므로 drop.
 - payload가 4000B를 초과하면 drop(SDIO 한계).
"""

import asyncio

from aiohttp import web, WSMsgType

import config
import cmd_channel
import audio_channel
from pacing import LiveRuntime
from logging_conf import get_logger

log = get_logger("ingest")

_BAD_PREFIXES = (b"OggS", b"OpusHead", b"OpusTags")


class IngestSource:
    """
    /ingest로 들어오는 실시간 Opus 프레임을 LiveRuntime에 공급하는 소스.
    LocalOpusSource와 동일한 async frames() 인터페이스를 가진다.
    """

    def __init__(self, maxlen=50):
        self._q = asyncio.Queue(maxsize=maxlen)
        self._closed = False
        self.received = 0
        self.dropped = 0

    async def push(self, frame):
        if self._closed:
            return
        try:
            self._q.put_nowait(frame)
        except asyncio.QueueFull:
            # 실시간성 우선: 가장 오래된 프레임을 버리고 최신을 넣는다.
            try:
                self._q.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
            self._q.put_nowait(frame)
        self.received += 1

    async def frames(self):
        """페이싱 producer가 호출. 프레임이 올 때까지 대기하며 yield."""
        while not self._closed:
            frame = await self._q.get()
            if frame is None:          # close 신호
                break
            yield frame

    def close(self):
        self._closed = True
        try:
            self._q.put_nowait(None)
        except asyncio.QueueFull:
            pass


def _check_token(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == config.INGEST_TOKEN
    return False


def _valid_frame(data):
    if not data or len(data) > config.OPUS_PAYLOAD_MAX:
        return False
    for p in _BAD_PREFIXES:
        if data.startswith(p):
            return False
    return True


async def ingest_handler(request):
    """GET /ingest (WebSocket). 군포 앱의 오디오 송출을 수신한다."""
    registry = request.app["registry"]
    session = request.app["session"]

    # 1) 인증
    if not _check_token(request):
        log.warning("[INGEST] unauthorized from %s", request.remote)
        raise web.HTTPUnauthorized(text="invalid ingest token")

    # 2) 동시 방송자 1명 제한 + 상태 확인
    if registry.has_ingest():
        log.warning("[INGEST] rejected: broadcaster already active")
        raise web.HTTPConflict(text="broadcaster already active")
    if not registry.all_cmd():
        raise web.HTTPServiceUnavailable(text="no device connected")
    if not session.can_start_live():
        raise web.HTTPConflict(text=f"busy: state={session.state.value}")

    ws = web.WebSocketResponse(heartbeat=config.WS_HEARTBEAT_SEC, max_msg_size=0)
    await ws.prepare(request)
    registry.set_ingest(ws)
    log.info("[INGEST] broadcaster connected from %s", request.remote)

    # 3) LIVE 시작: 상태 전이 + LIVE_START + 런타임 준비
    source = IngestSource()
    session_id = session.start_live()
    cmd_id = session.next_cmd_id()
    await cmd_channel.broadcast_to_devices(
        registry,
        cmd_channel.build_live_start(
            cmd_id, session_id, frame_ms=config.DEFAULT_FRAME_MS,
            record_flash=0, file_name="live.lopus"))

    fanout = audio_channel.make_fanout(registry)
    runtime = LiveRuntime(frame_ms=config.DEFAULT_FRAME_MS,
                          source=source, fanout=fanout, loop_source=False)
    request.app["live"] = runtime

    # /audio 접속을 기다렸다가 페이싱 시작 (http_api._run_live 재사용)
    from http_api import _run_live
    asyncio.create_task(_run_live(request.app, runtime, config.READY_TIMEOUT_DEFAULT))

    # 4) 수신 루프: binary 프레임을 소스에 push
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if _valid_frame(msg.data):
                    await source.push(msg.data)
                else:
                    log.warning("[INGEST] dropped invalid frame len=%d",
                                len(msg.data) if msg.data else 0)
            elif msg.type == WSMsgType.ERROR:
                log.warning("[INGEST] ws error: %s", ws.exception())
    finally:
        # 5) 방송 종료: 소스 닫고 런타임 정지, LIVE_STOP, 세션 정리
        source.close()
        registry.clear_ingest(ws)
        runtime_now = request.app.get("live")
        if runtime_now is runtime:
            await runtime.stop()
            request.app["live"] = None
            stop_cid = session.next_cmd_id()
            await cmd_channel.broadcast_to_devices(
                registry, cmd_channel.build_live_stop(stop_cid, session.session_id))
            session.stop_live()
        log.info("[INGEST] broadcaster disconnected (recv=%d dropped=%d)",
                 source.received, source.dropped)

    return ws