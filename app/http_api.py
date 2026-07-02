"""
http_api.py — HTTP 엔드포인트.

  POST /upload     군포 앱이 MP3를 업로드한다. 저장 후 메타를 응답.
  POST /broadcast  업로드된 파일의 단말 전송을 트리거한다(FILE_START).
  GET  /api/files  업로드된 파일 목록.
  GET  /api/health 상태 조회 (server.py에 정의).

파일 다운로드 자체는 nginx가 /files/ 로 정적 서빙하므로 여기서 다루지 않는다.
단말은 FILE_START의 https_url로 직접 당겨받는다.
"""

import asyncio
import json
import os
import time

from aiohttp import web

import config
import cmd_channel
import audio_channel
from pacing import LiveRuntime
from opus_source import LocalOpusSource
from file_manager import FileTooLarge, BadFileType
from logging_conf import get_logger

log = get_logger("http_api")


async def upload_handler(request):
    """POST /upload — multipart/form-data 의 'file' 필드를 저장."""
    fmgr = request.app["file_manager"]

    if not request.content_type.startswith("multipart/"):
        return web.json_response(
            {"ok": False, "error": "expected multipart/form-data"}, status=400)

    reader = await request.multipart()
    field = await reader.next()
    # 'file' 필드를 찾는다.
    while field is not None and field.name != "file":
        field = await reader.next()
    if field is None:
        return web.json_response(
            {"ok": False, "error": "no 'file' field"}, status=400)

    try:
        meta = await fmgr.save_stream(field.filename, field)
    except BadFileType as e:
        return web.json_response({"ok": False, "error": str(e)}, status=415)
    except FileTooLarge as e:
        return web.json_response({"ok": False, "error": str(e)}, status=413)

    return web.json_response({
        "ok": True,
        "file_name": meta["file_name"],
        "size": meta["size"],
        "sha256": meta["sha256"],
        "https_url": meta["https_url"],
    })


async def broadcast_handler(request):
    """
    POST /broadcast — FILE_START 트리거.
    본문(JSON): {file_name, device?, store_flash?, autoplay?}
      device 생략 시 연결된 모든 단말에 전송.
    """
    fmgr = request.app["file_manager"]
    registry = request.app["registry"]
    session = request.app["session"]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"ok": False, "error": "invalid JSON body"}, status=400)

    file_name = body.get("file_name")
    if not file_name:
        return web.json_response(
            {"ok": False, "error": "file_name required"}, status=400)

    meta = fmgr.get(file_name)
    if meta is None:
        return web.json_response(
            {"ok": False, "error": "unknown file_name"}, status=404)

    # 연결된 단말이 있는지 확인
    if not registry.all_cmd():
        return web.json_response(
            {"ok": False, "error": "no device connected"}, status=503)

    # 상태 규칙: LIVE 중이면 FILE 불가
    if not session.can_start_file():
        st = session.state.value
        if st == "LIVE":
            msg_txt = "라이브 방송 중에는 파일을 보낼 수 없습니다."
        else:
            msg_txt = "이전 파일 방송이 아직 처리 중입니다. 잠시 후 다시 시도하세요."
        return web.json_response(
            {"ok": False, "error": msg_txt, "state": st, "busy": True},
            status=409)

    file_id = session.start_file()
    cmd_id = session.next_cmd_id()
    # 단말에 보내는 재방송 저장 파일명은 전송 시각 기준 epoch로 매번 새로 만든다.
    # (원본 저장명과 무관하게, 이번 전송을 식별하는 이름)
    ext = os.path.splitext(meta["file_name"])[1] or ".mp3"
    send_file_name = f"file-{int(time.time())}{ext}"
    payload = cmd_channel.build_file_start(
        cmd_id=cmd_id,
        file_id=file_id,
        https_url=meta["https_url"],
        size=meta["size"],
        sha256=meta["sha256"],
        file_name=send_file_name,
        store_flash=bool(body.get("store_flash", True)),
        autoplay=bool(body.get("autoplay", True)),
    )

    # 대상 단말: devices(목록) 우선, 없으면 device(단일), 둘 다 없으면 전체
    target_devices = body.get("devices")
    single_device = body.get("device")
    try:
        if target_devices:
            sent_count = 0
            for dev in target_devices:
                if await cmd_channel.send_to_device(registry, dev, payload):
                    sent_count += 1
        elif single_device:
            sent = await cmd_channel.send_to_device(registry, single_device, payload)
            sent_count = 1 if sent else 0
        else:
            sent_count = await cmd_channel.broadcast_to_devices(registry, payload)
    except Exception as e:
        session.stop_file()   # 송신 실패 시 상태 롤백
        log.warning("[FILE] broadcast send error: %s", e)
        return web.json_response(
            {"ok": False, "error": "send failed"}, status=500)

    if sent_count == 0:
        session.stop_file()   # 아무에게도 못 보냄 → 롤백
        return web.json_response(
            {"ok": False, "error": "device not reachable"}, status=503)

    return web.json_response({
        "ok": True,
        "cmd_id": cmd_id,
        "file_id": file_id,
        "sent_to": sent_count,
        "https_url": meta["https_url"],
    })


async def files_handler(request):
    """GET /api/files — 업로드된 파일 목록."""
    fmgr = request.app["file_manager"]
    return web.json_response({"ok": True, "files": fmgr.list_files()})


async def file_delete_handler(request):
    """DELETE /api/files/{file_name} — 업로드 파일 삭제."""
    fmgr = request.app["file_manager"]
    file_name = request.match_info.get("file_name", "")
    if fmgr.delete(file_name):
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "unknown file_name"}, status=404)


# ── LIVE 제어 ──────────────────────────────────────────────

async def _run_live(app, runtime, ready_timeout_sec):
    """
    라이브 수명 관리(백그라운드).
     1) C6가 /audio에 접속할 때까지 대기(사양 08). 타임아웃이면 abort.
     2) 페이싱 시작.
     3) 소스가 소진되어 자연 종료하면(로컬 파일 1회 재생) 세션을 정리한다.
        명시적 live/stop이 먼저 일어났으면(app["live"] 교체됨) 아무것도 하지 않는다.
    실시간 인제스트(4단계)는 소스가 무한이라 3)이 발동하지 않는다.
    """
    registry = app["registry"]
    session = app["session"]

    # 1) /audio 접속 대기
    deadline = time.monotonic() + ready_timeout_sec
    while time.monotonic() < deadline:
        if registry.audio_count() > 0:
            break
        await asyncio.sleep(0.05)
    else:
        log.warning("[LIVE] no /audio within %ds, aborting live", ready_timeout_sec)
        if app.get("live") is runtime:
            cid = session.next_cmd_id()
            await cmd_channel.broadcast_to_devices(
                registry, cmd_channel.build_live_stop(cid, session.session_id))
            session.stop_live()
            app["live"] = None
            # 군포(ingest)가 연결돼 있으면 함께 정리한다.
            ing = registry.get_ingest()
            if ing is not None and not ing.closed:
                await ing.close()
        return

    # 2) 페이싱 시작
    await runtime.start()
    log.info("[LIVE] audio connected, pacing started")

    # 3) 자연 종료 대기 후 정리
    await runtime.wait_done()
    if app.get("live") is runtime:
        cid = session.next_cmd_id()
        await cmd_channel.broadcast_to_devices(
            registry, cmd_channel.build_live_stop(cid, session.session_id))
        session.stop_live()
        app["live"] = None
        log.info("[LIVE] natural end, session closed")


async def live_start_handler(request):
    """
    POST /api/live/start — 라이브 방송 시작.
    본문(JSON, 선택): {source?, frame_ms?, record_flash?, loop?}
      source 생략 시 샘플 voice001_mono_40ms.opus 사용.
    """
    registry = request.app["registry"]
    session = request.app["session"]

    if request.can_read_body:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
    else:
        body = {}

    if not registry.all_cmd():
        return web.json_response(
            {"ok": False, "error": "no device connected"}, status=503)
    if not session.can_start_live():
        return web.json_response(
            {"ok": False, "error": f"busy: state={session.state.value}"},
            status=409)

    # 소스 결정 (3단계: 서버 로컬 .opus)
    source_name = body.get("source", "voice001_mono_40ms.opus")
    source_path = os.path.join(config.SAMPLE_DIR, source_name)
    if not os.path.isfile(source_path):
        return web.json_response(
            {"ok": False, "error": f"source not found: {source_name}"}, status=404)

    frame_ms = body.get("frame_ms") or config.DEFAULT_FRAME_MS
    record_flash = int(body.get("record_flash", 0))
    loop_source = bool(body.get("loop", False))
    ready_timeout = config.READY_TIMEOUT_DEFAULT

    # 상태 전이 + LIVE_START 송신 (FILE 중이면 선점)
    session_id = session.start_live()
    cmd_id = session.next_cmd_id()
    await cmd_channel.broadcast_to_devices(
        registry,
        cmd_channel.build_live_start(
            cmd_id, session_id, frame_ms=frame_ms,
            record_flash=record_flash, file_name="live.lopus"))

    # 런타임 준비: 소스 + 페이싱 + 팬아웃
    source = LocalOpusSource(source_path, loop=loop_source)
    fanout = audio_channel.make_fanout(registry)
    runtime = LiveRuntime(frame_ms=frame_ms, source=source,
                          fanout=fanout, loop_source=loop_source)
    request.app["live"] = runtime

    # /audio 접속을 기다렸다가 페이싱 시작, 자연 종료 시 정리 (백그라운드)
    asyncio.create_task(
        _run_live(request.app, runtime, ready_timeout))

    return web.json_response({
        "ok": True,
        "session_id": session_id,
        "cmd_id": cmd_id,
        "frame_ms": frame_ms,
        "source": source_name,
        "source_packets": source.packet_count,
    })


async def live_stop_handler(request):
    """POST /api/live/stop — 라이브 방송 종료."""
    registry = request.app["registry"]
    session = request.app["session"]
    runtime = request.app.get("live")

    if runtime is not None:
        await runtime.stop()
        request.app["live"] = None

    cmd_id = session.next_cmd_id()
    await cmd_channel.broadcast_to_devices(
        registry, cmd_channel.build_live_stop(cmd_id, session.session_id))
    stats = runtime.stats() if runtime else None
    session.stop_live()

    return web.json_response({"ok": True, "cmd_id": cmd_id, "stats": stats})


async def server_restart_handler(request):
    """POST /api/server/restart — 서버 프로세스를 재시작한다.

    프로세스가 스스로 종료하면 systemd(Restart=always)가 자동으로 되살린다.
    응답을 먼저 보낸 뒤, 짧은 지연을 두고 종료한다.
    데모용 수동 복구 버튼. 관리자 인증(JWT 미들웨어)을 통과해야만 호출된다.
    """
    log.warning("[SERVER] restart requested by admin — 프로세스 재시작")

    async def _delayed_exit():
        await asyncio.sleep(0.5)   # 응답 전송 시간 확보
        # 현재 라이브/파일 세션 정리
        runtime = request.app.get("live")
        if runtime is not None:
            try:
                await runtime.stop()
            except Exception:
                pass
        # systemd가 되살리도록 프로세스 종료
        os._exit(0)

    asyncio.create_task(_delayed_exit())
    return web.json_response({"ok": True, "message": "서버를 재시작합니다. 잠시 후 자동으로 복구됩니다."})