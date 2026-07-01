"""
audio_channel.py — /audio WSS 오디오 팬아웃 채널.

역할:
 - 단말(C6)이 LIVE_START 이후 /audio에 접속하면 레지스트리에 등록한다.
   (사양: C6는 P4 READY 후에 /audio를 연다. 서버는 접속을 기다린다.)
 - 페이싱 엔진이 내보내는 Opus packet을 연결된 모든 단말에 팬아웃한다.
 - WSS binary 1개 = Opus packet 1개. 절대 합치지 않는다.

팬아웃은 "한 번 인코딩, 여러 번 전송" 원칙을 따른다. 동일한 packet
바이트를 모든 단말에 그대로 보낸다(단말별 재인코딩 없음).
"""

from aiohttp import web, WSMsgType

import config
from logging_conf import get_logger

log = get_logger("audio")


async def audio_handler(request):
    """GET /audio (WebSocket). 단말의 오디오 채널 접속을 수락한다."""
    registry = request.app["registry"]
    device_id = request.query.get("device") or request.remote or "unknown"

    ws = web.WebSocketResponse(heartbeat=config.WS_HEARTBEAT_SEC, max_msg_size=0)
    await ws.prepare(request)
    registry.add_audio(device_id, ws)
    log.info("[AUDIO] connected device=%s targets=%d",
             device_id, registry.audio_count())

    try:
        # 단말은 보통 오디오 채널로 데이터를 보내지 않는다.
        # 연결 유지와 종료 감지를 위해 수신 루프를 돌며, 단말이 보내는
        # 하트비트(텍스트 ping 또는 표준 ping 프레임)에는 응답한다.
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                text = (msg.data or "").strip()
                try:
                    reply = "pong" if text.lower() == "ping" else text
                    await ws.send_str(reply)
                except Exception:
                    pass
            elif msg.type == WSMsgType.PING:
                await ws.pong(msg.data)
            elif msg.type == WSMsgType.ERROR:
                log.warning("[AUDIO] ws error device=%s: %s",
                            device_id, ws.exception())
    finally:
        registry.remove_audio(device_id, ws)
        log.info("[AUDIO] disconnected device=%s", device_id)

    return ws


def make_fanout(registry):
    """
    페이싱 엔진에 넘길 async fanout 콜백을 만든다.
    연결된 모든 audio WSS에 Opus packet 1개를 binary로 보낸다.
    전송이 실패한 단말은 닫고 레지스트리에서 정리한다.
    """
    async def fanout(frame_bytes):
        dead = []
        for device_id, ws in registry.all_audio():
            if ws.closed:
                dead.append((device_id, ws))
                continue
            try:
                await ws.send_bytes(frame_bytes)
            except Exception as e:
                log.warning("[AUDIO] send failed device=%s: %s", device_id, e)
                dead.append((device_id, ws))
        for device_id, ws in dead:
            registry.remove_audio(device_id, ws)

    return fanout