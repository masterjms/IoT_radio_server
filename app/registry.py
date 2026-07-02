"""
registry.py — 연결 레지스트리.

device_id → WebSocket 매핑을 관리한다.
 - CMD 채널에 붙은 단말(C6)
 - AUDIO 채널에 붙은 단말(C6)
 - INGEST 채널에 붙은 군포 앱

CMD와 AUDIO는 같은 단말이라도 서로 다른 WebSocket이므로 분리해 보관한다.

재접속 정책:
 같은 device_id로 새 접속이 오면, 이전 WebSocket을 닫고 새 것으로 교체한다.
 ("새 연결이 이전 연결을 선점")
 이렇게 해야 네트워크 순단 후 재접속 시 레지스트리에 좀비 항목이 남지 않는다.

device_id 안정성:
 C6는 재접속해도 동일한 ID를 보내야 한다. 권장: WiFi MAC 주소.
 C6 펌웨어에서 esp_wifi_get_mac()로 MAC을 읽어 접속 URL에 포함시킨다.
 예: wss://iotradio.co.kr/cmd?device=aa:bb:cc:dd:ee:ff
"""

import asyncio
import time

from logging_conf import get_logger

log = get_logger("registry")


class Registry:
    def __init__(self):
        self._cmd = {}            # device_id -> ws (제어 채널)
        self._cmd_since = {}      # device_id -> connected_at(epoch)
        self._audio_socks = []    # [(device_id, ws)] 오디오 채널 (소켓 단위)
        self._ingest = None       # 현재 활성 방송자 ws (단일)

    # ── 내부: 기존 연결 선점 닫기 ─────────────────────────
    @staticmethod
    async def _close_old(old_ws, device_id, channel):
        """이전 WebSocket을 비동기로 닫는다. 이미 닫혔으면 조용히 무시.
        빠른 재연결이 반복될 때 close가 오래 매달리지 않도록 타임아웃을 둔다."""
        if old_ws is None or old_ws.closed:
            return
        try:
            await asyncio.wait_for(old_ws.close(), timeout=3.0)
            log.info("[WSS] %s preempted old connection device=%s", channel, device_id)
        except asyncio.TimeoutError:
            log.warning("[WSS] %s old close timeout device=%s (강제 무시)",
                        channel, device_id)
        except Exception as e:
            log.debug("[WSS] %s close old error device=%s: %s", channel, device_id, e)

    # ── CMD ────────────────────────────────────────────────
    def add_cmd(self, device_id, ws):
        old = self._cmd.get(device_id)
        self._cmd[device_id] = ws
        self._cmd_since[device_id] = time.time()
        if old is not None and not old.closed:
            # 이전 연결 닫기는 비동기라 태스크로 띄운다.
            asyncio.create_task(self._close_old(old, device_id, "cmd"))
            log.warning("[WSS] cmd reconnect device=%s (old replaced)", device_id)
        else:
            log.info("[WSS] cmd registered device=%s total=%d",
                     device_id, len(self._cmd))

    def remove_cmd(self, device_id, ws=None):
        """
        ws를 지정하면, 현재 등록된 ws와 동일할 때만 제거한다.
        재접속으로 이미 새 ws가 등록된 경우 이전 ws의 finally가 잘못 지우는 것을 막는다.
        """
        current = self._cmd.get(device_id)
        if current is None:
            return
        if ws is not None and current is not ws:
            # 이미 새 연결이 들어와 있음 → 건드리지 않는다
            log.debug("[WSS] cmd remove skipped device=%s (new ws already registered)", device_id)
            return
        del self._cmd[device_id]
        log.info("[WSS] cmd removed device=%s total=%d", device_id, len(self._cmd))

    def get_cmd(self, device_id):
        return self._cmd.get(device_id)

    def all_cmd(self):
        return list(self._cmd.items())

    def device_list(self):
        """
        단말 목록 스냅샷. UI 표시용.
        connected: 현재 cmd WSS가 살아있는지
        audio_connected: 현재 audio WSS가 살아있는지(LIVE 중에만 의미 있음)
        since: cmd 채널이 마지막으로 (재)연결된 epoch 초
        """
        out = []
        audio_ids = {d for (d, w) in self._audio_socks if not w.closed}
        for device_id, ws in self._cmd.items():
            out.append({
                "device_id": device_id,
                "connected": not ws.closed,
                "audio_connected": device_id in audio_ids,
                "since": self._cmd_since.get(device_id),
            })
        out.sort(key=lambda d: d["device_id"])
        return out

    # ── AUDIO ──────────────────────────────────────────────
    def add_audio(self, device_id, ws):
        # audio는 device_id로 식별하지 않고 소켓 단위로 관리한다.
        # C6가 /audio 접속 시 ?device=<MAC>를 붙이지 않아 여러 단말이
        # 같은 IP(127.0.0.1)로 들어오는데, device_id로 키를 잡으면 서로
        # 밀어내 한 대만 남는다. 소켓 집합으로 두면 모두 유지되어 팬아웃된다.
        self._audio_socks.append((device_id, ws))
        log.info("[WSS] audio registered device=%s total=%d",
                 device_id, len(self._audio_socks))

    def remove_audio(self, device_id, ws=None):
        before = len(self._audio_socks)
        self._audio_socks = [
            (d, w) for (d, w) in self._audio_socks if w is not ws
        ]
        if len(self._audio_socks) != before:
            log.info("[WSS] audio removed device=%s total=%d",
                     device_id, len(self._audio_socks))

    def all_audio(self):
        return list(self._audio_socks)

    def audio_count(self):
        return len(self._audio_socks)

    # ── INGEST (군포 앱) ───────────────────────────────────
    def set_ingest(self, ws):
        old = self._ingest
        self._ingest = ws
        if old is not None and not old.closed:
            asyncio.create_task(self._close_old(old, "ingest", "ingest"))

    def clear_ingest(self, ws):
        if self._ingest is ws:
            self._ingest = None

    def get_ingest(self):
        return self._ingest

    def has_ingest(self):
        return self._ingest is not None