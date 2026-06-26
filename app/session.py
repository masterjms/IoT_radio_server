"""
session.py — 방송 상태 머신.

사양 규칙(06 문서):
 - LIVE와 FILE은 동시에 수행하지 않는다.
 - FILE 진행 중 LIVE_START가 오면 FILE을 중단하고 LIVE가 우선한다.

상태: IDLE / LIVE / FILE
이 모듈은 "지금 무엇을 할 수 있는가"를 판단하는 권한자다.
실제 명령 송신은 cmd_channel이 담당하고, 여기서는 전이 가부만 정한다.
"""

import enum
import threading

from logging_conf import get_logger

log = get_logger("session")


class State(enum.Enum):
    IDLE = "IDLE"
    LIVE = "LIVE"
    FILE = "FILE"


class SessionManager:
    def __init__(self):
        self._state = State.IDLE
        self._session_id = 0      # LIVE 세션 식별자 (증가)
        self._file_id = 0         # FILE 식별자 (증가)
        self._cmd_id = 0          # 명령 일련번호 (증가)
        self._lock = threading.Lock()

    @property
    def state(self):
        return self._state

    def next_cmd_id(self):
        with self._lock:
            self._cmd_id += 1
            return self._cmd_id

    # ── LIVE ───────────────────────────────────────────────
    def can_start_live(self):
        """LIVE는 IDLE 또는 FILE(선점) 상태에서 시작 가능. 이미 LIVE면 거부."""
        return self._state in (State.IDLE, State.FILE)

    def start_live(self):
        """LIVE 시작. FILE 중이었다면 선점된 것으로 본다. 새 session_id 반환."""
        with self._lock:
            preempted = self._state == State.FILE
            self._state = State.LIVE
            self._session_id += 1
            sid = self._session_id
        if preempted:
            log.info("[STATE] FILE preempted by LIVE")
        log.info("[STATE] -> LIVE session=%d", sid)
        return sid

    def stop_live(self):
        with self._lock:
            if self._state == State.LIVE:
                self._state = State.IDLE
        log.info("[STATE] LIVE -> IDLE")

    # ── FILE ───────────────────────────────────────────────
    def can_start_file(self):
        """FILE은 IDLE에서만 시작 가능. LIVE 중이면 거부."""
        return self._state == State.IDLE

    def start_file(self):
        """FILE 시작. 새 file_id 반환."""
        with self._lock:
            self._state = State.FILE
            self._file_id += 1
            fid = self._file_id
        log.info("[STATE] -> FILE file_id=%d", fid)
        return fid

    def stop_file(self):
        with self._lock:
            if self._state == State.FILE:
                self._state = State.IDLE
        log.info("[STATE] FILE -> IDLE")

    @property
    def session_id(self):
        return self._session_id

    @property
    def file_id(self):
        return self._file_id

    def snapshot(self):
        """상태 조회용 (/api/health 등)."""
        return {
            "state": self._state.value,
            "session_id": self._session_id,
            "file_id": self._file_id,
        }