"""
cmd_channel.py — /cmd WSS 제어 채널.

역할:
 - 단말(C6)의 CMD WSS 연결을 수락하고 레지스트리에 등록한다. (상시 유지)
 - 서버 -> C6 명령(JSON)을 송신한다: LIVE_START, LIVE_STOP, FILE_START, FILE_STOP
 - C6 -> 서버 보고(JSON)를 수신한다: FILE_END, FILE_ABORT, LIVE_STATS, LIVE_READY
 - ping/pong heartbeat로 끊긴 연결을 정리한다.

JSON 포맷은 사양 06 문서를 그대로 따른다.
서버는 C6와만 대화하며, P4와 직접 통신하지 않는다.

주의: C6가 자신의 device_id를 서버에 알리는 방법은 사양에 명시되어
있지 않다(신규 설계 영역). 여기서는 연결 URL의 query 파라미터
`?device=<id>`로 받는다고 가정한다. 없으면 원격주소를 임시 id로 쓴다.
"""

import json

from aiohttp import web, WSMsgType

import config
from session import State
from logging_conf import get_logger

log = get_logger("cmd")


# ── 서버 -> C6 명령 빌더 (사양 06) ─────────────────────────

def build_live_start(cmd_id, session_id, *, frame_ms=None, sample_rate=None,
                     ready_timeout_sec=None, record_flash=0,
                     file_name="live.lopus", mode="LOW_LATENCY", codec="opus"):
    # 보정 규칙 적용 (06 문서: 비정상 값이면 기본값으로)
    if not frame_ms or frame_ms <= 0:
        frame_ms = config.DEFAULT_FRAME_MS
    if not sample_rate or sample_rate <= 0:
        sample_rate = config.DEFAULT_SAMPLE_RATE
    if ready_timeout_sec is None or not (
            config.READY_TIMEOUT_MIN <= ready_timeout_sec <= config.READY_TIMEOUT_MAX):
        ready_timeout_sec = config.READY_TIMEOUT_DEFAULT
    return {
        "type": "LIVE_START",
        "ver": config.PROTO_VER,
        "cmd_id": cmd_id,
        "session_id": session_id,
        "frame_ms": frame_ms,
        "ready_timeout_sec": ready_timeout_sec,
        "sample_rate": sample_rate,
        "mode": mode,
        "codec": codec,
        "record_flash": int(record_flash),
        "file_name": file_name,
    }


def build_live_stop(cmd_id, session_id, *, mode="IMMEDIATE",
                    drain_timeout_ms=2000, fade_ms=200):
    return {
        "type": "LIVE_STOP",
        "ver": config.PROTO_VER,
        "cmd_id": cmd_id,
        "session_id": session_id,
        "mode": mode,
        "drain_timeout_ms": drain_timeout_ms,
        "fade_ms": fade_ms,
    }


def build_file_start(cmd_id, file_id, *, https_url, size, sha256,
                     file_name, store_flash=True, autoplay=True, resume_offset=0):
    return {
        "type": "FILE_START",
        "ver": config.PROTO_VER,
        "cmd_id": cmd_id,
        "file_id": file_id,
        "https_url": https_url,
        "size": size,
        "sha256": sha256,
        "store_flash": bool(store_flash),
        "autoplay": bool(autoplay),
        "file_name": file_name,
        "resume_offset": resume_offset,
    }


def build_file_stop(cmd_id, file_id):
    return {
        "type": "FILE_STOP",
        "ver": config.PROTO_VER,
        "cmd_id": cmd_id,
        "file_id": file_id,
    }


# ── 송신 헬퍼 ──────────────────────────────────────────────

async def send_to_device(registry, device_id, payload):
    """특정 단말의 CMD WSS로 JSON 명령을 보낸다."""
    ws = registry.get_cmd(device_id)
    if ws is None or ws.closed:
        log.warning("[CMD] send failed: device=%s not connected", device_id)
        return False
    await ws.send_str(json.dumps(payload))
    log.info("[CMD] -> %s %s cmd_id=%s",
             device_id, payload.get("type"), payload.get("cmd_id"))
    return True


async def broadcast_to_devices(registry, payload):
    """연결된 모든 단말에 동일 명령을 보낸다. (단일 단말이면 그 하나에만)"""
    sent = 0
    for device_id, ws in registry.all_cmd():
        if not ws.closed:
            await ws.send_str(json.dumps(payload))
            sent += 1
    log.info("[CMD] broadcast %s to %d device(s)", payload.get("type"), sent)
    return sent


# ── C6 -> 서버 보고 처리 ───────────────────────────────────

def _is_preempt_abort(msg):
    """FILE_ABORT가 LIVE 선점(PREEMPTED_BY_LIVE)에 의한 것인지 판정."""
    # 06 문서: reason 0x02 = PREEMPTED_BY_LIVE
    if msg.get("reason") == 0x02:
        return True
    return str(msg.get("fail_reason", "")).upper() == "PREEMPTED_BY_LIVE"


def _is_busy_abort(msg):
    """FILE_ABORT가 BUSY(단말이 파일 방송 처리 중)인지 판정."""
    # 가이드: reason 0x08 또는 문자열 "BUSY"
    r = msg.get("reason")
    if r == 0x08 or r == "BUSY":
        return True
    return str(msg.get("fail_reason", "")).upper() == "BUSY"


async def _handle_report(device_id, msg, app):
    """C6가 올린 JSON 보고를 해석한다."""
    session = app["session"]
    registry = app["registry"]
    mtype = msg.get("type")

    if mtype == "FILE_END":
        ok = msg.get("verify_ok")
        if ok:
            log.info("[FILE] end result=0x00 device=%s file_id=%s OK",
                     device_id, msg.get("file_id"))
        else:
            log.warning("[FILE] end device=%s file_id=%s FAIL reason=%s",
                        device_id, msg.get("file_id"), msg.get("fail_reason"))
        session.stop_file()

    elif mtype == "FILE_ABORT":
        reason = msg.get("reason")
        fail = msg.get("fail_reason")
        if _is_busy_abort(msg):
            # BUSY: 단말이 파일 방송 처리 중이라 새 방송을 받을 수 없는
            # 임시 거절. 오류가 아니므로 FILE 상태만 즉시 정리해서, 사용자가
            # 다시 전송 버튼을 누르면 재시도할 수 있게 한다.
            log.info("[FILE] abort BUSY device=%s file_id=%s — 상태 정리, 재시도 가능",
                     device_id, msg.get("file_id"))
            session.stop_file()
            # 프론트가 조회할 수 있도록 마지막 결과를 기록
            app["last_file_result"] = {"status": "busy", "device": device_id}
        elif _is_preempt_abort(msg):
            log.info("[FILE] abort PREEMPTED_BY_LIVE device=%s — LIVE 상태 유지",
                     device_id)
            # LIVE가 이미 시작됐으므로 FILE 종료 처리를 하지 않는다.
        else:
            log.warning("[FILE] abort device=%s file_id=%s reason=%s last_offset=%s",
                        device_id, msg.get("file_id"), fail or reason,
                        msg.get("last_offset"))
            session.stop_file()
            app["last_file_result"] = {"status": "abort", "device": device_id,
                                       "reason": str(fail or reason)}

    elif mtype == "LIVE_STATS":
        # 페이싱 엔진이 참고할 관측값. 지금은 로그만.
        log.info("[LIVE] stats device=%s p4_buffer_ms=%s underrun=%s seq=%s",
                 device_id, msg.get("p4_buffer_ms"),
                 msg.get("underrun_count"), msg.get("rx_seq_last"))

    elif mtype == "LIVE_READY":
        status = msg.get("status", 0)
        if status == 0:
            log.info("[LIVE] ready OK device=%s session=%s",
                     device_id, msg.get("session_id"))
        else:
            # P4 준비 실패/타임아웃 → 진행 중이던 LIVE를 정리한다.
            log.warning("[LIVE] P4 ready FAILED device=%s status=%s reason=%s",
                        device_id, status, msg.get("reason"))
            runtime = app.get("live")
            if runtime is not None:
                await runtime.stop()
                app["live"] = None
            if session.state == State.LIVE:
                cid = session.next_cmd_id()
                await broadcast_to_devices(
                    registry, build_live_stop(cid, session.session_id))
                session.stop_live()
            # 군포(ingest) 연결도 정리한다.
            ing = registry.get_ingest()
            if ing is not None and not ing.closed:
                await ing.close()

    else:
        log.info("[CMD] <- %s unknown/other type=%s", device_id, mtype)


# ── LIVE 복구 (재접속/늦은 합류) ───────────────────────────

async def _resync_live(app, registry, session, device_id):
    """
    현재 LIVE 중이면 방금 접속한 단말에 LIVE_START를 다시 보낸다.
    그러면 C6가 P4를 준비하고 /audio에 (재)접속해 방송에 합류한다.
    페이싱 fanout은 매 프레임 all_audio()를 조회하므로, 새 audio가
    등록되는 즉시 자동으로 그 단말에도 전송된다.
    """
    runtime = app.get("live")
    if session.state == State.LIVE and runtime is not None:
        cid = session.next_cmd_id()
        payload = build_live_start(
            cid, session.session_id,
            frame_ms=runtime.engine.frame_ms,
            record_flash=0, file_name="live.lopus")
        await send_to_device(registry, device_id, payload)
        log.info("[LIVE] resync LIVE_START -> device=%s session=%d",
                 device_id, session.session_id)


# ── WSS 핸들러 ─────────────────────────────────────────────

async def cmd_handler(request):
    """aiohttp 라우트 핸들러: GET /cmd (WebSocket)."""
    registry = request.app["registry"]
    session = request.app["session"]

    device_id = request.query.get("device") or request.remote or "unknown"

    ws = web.WebSocketResponse(
        heartbeat=config.WS_HEARTBEAT_SEC,
        timeout=config.WS_HEARTBEAT_TIMEOUT,
    )
    await ws.prepare(request)
    registry.add_cmd(device_id, ws)
    log.info("[WSS] cmd connected device=%s", device_id)

    # LIVE 진행 중이면 이 단말에 LIVE_START를 재공지한다.
    # (재접속 복구 + 늦게 합류한 단말의 방송 참여)
    await _resync_live(request.app, registry, session, device_id)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    # JSON이 아닌 텍스트는 단말의 하트비트(ping)로 간주하고
                    # 응답(pong)을 돌려준다. 응답이 없으면 단말이 연결을
                    # 죽은 것으로 보고 주기적으로 끊는다.
                    text = (msg.data or "").strip()
                    log.info("[CMD] <- %s heartbeat text=%r -> pong",
                             device_id, text[:32])
                    try:
                        # 단말이 "ping"을 보내면 "pong"으로, 그 외 텍스트는
                        # 같은 내용으로 에코해 살아있음을 알린다.
                        reply = "pong" if text.lower() == "ping" else text
                        await ws.send_str(reply)
                    except Exception:
                        pass
                    continue
                await _handle_report(device_id, payload, request.app)
            elif msg.type == WSMsgType.PING:
                # 표준 WebSocket ping 프레임 → 표준 pong으로 응답
                await ws.pong(msg.data)
            elif msg.type == WSMsgType.ERROR:
                log.warning("[WSS] cmd error device=%s: %s",
                            device_id, ws.exception())
    finally:
        registry.remove_cmd(device_id, ws)
        log.info("[WSS] cmd disconnected device=%s", device_id)

    return ws