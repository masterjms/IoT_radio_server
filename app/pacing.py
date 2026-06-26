"""
pacing.py — LIVE Opus 송신 페이싱 엔진 (사양 07 정책).

목적:
 - 평상시: frame_ms 간격으로 1개씩 송신한다.
 - 송신이 밀리면(backlog) 제한형 burst로 따라잡는다.
 - 너무 늦은 frame은 폐기한다.

07 정책 기준값(40ms frame):
 - expected_send_time = live_start_time + frame_index * frame_ms
 - backlog 0~1 frame   : 정상 pacing
 - backlog 2~3 frame   : 제한형 catch-up
 - backlog 4+ frame    : catch-up, 단 1회 burst group <= 120ms(3 frame)
 - backlog > 1600 ms   : 오래된 frame 폐기 시작
 - backlog > 2000 ms   : 반드시 폐기
 - WSS binary 1개 = Opus packet 1개 (절대 합치지 않음)

핵심 로직은 _step(now_ms)에 순수 함수로 모았다. 시계를 주입하면
시간을 제어하며 backlog/burst/drop을 단위 테스트할 수 있다.
"""

import asyncio
import time
from collections import deque

from logging_conf import get_logger

log = get_logger("pacing")

# 07 정책 상수
BACKLOG_PRESERVE_MS = 1600    # 이 이상이면 오래된 frame drop 시작
BACKLOG_DISCARD_MS = 2000     # 이 이상이면 반드시 폐기
BURST_GROUP_MAX_MS = 120      # 1회 burst group 최대 오디오 시간


def _now_ms():
    return time.monotonic() * 1000.0


class PacingStats:
    def __init__(self):
        self.sent = 0
        self.dropped_old = 0        # age > 2000ms 폐기
        self.dropped_backlog = 0    # backlog > 1600ms 폐기
        self.bursts = 0             # burst(2개 이상 동시 송신) 횟수

    def snapshot(self):
        return {
            "sent": self.sent,
            "dropped_old": self.dropped_old,
            "dropped_backlog": self.dropped_backlog,
            "bursts": self.bursts,
        }


class PacingEngine:
    def __init__(self, frame_ms, fanout, *, queue_max=96, clock=_now_ms):
        """
        frame_ms : 프레임 간격(ms)
        fanout   : async callable(frame_bytes) — 실제 전송(팬아웃)
        queue_max: 내부 큐 상한(백프레셔). 07 기준 96 frame.
        clock    : 단조 시계(ms) 함수. 테스트에서 주입.
        """
        self.frame_ms = frame_ms
        self._fanout = fanout
        self._qmax = queue_max
        self._clock = clock
        self.max_burst_frames = max(1, BURST_GROUP_MAX_MS // frame_ms)

        self._q = deque()           # (frame_index, frame_bytes)
        self._submit_idx = 0
        self._start_ms = None
        self._active = False
        self.stats = PacingStats()

    # ── 수명 ───────────────────────────────────────────────
    def start(self):
        self._start_ms = self._clock()
        self._active = True
        log.info("[LIVE] pacing start frame_ms=%d max_burst=%d",
                 self.frame_ms, self.max_burst_frames)

    @property
    def active(self):
        return self._active

    @property
    def queue_len(self):
        return len(self._q)

    def _expected(self, frame_index):
        return self._start_ms + frame_index * self.frame_ms

    # ── 소스 → 엔진 ────────────────────────────────────────
    async def submit(self, frame):
        """프레임을 큐에 넣는다. 큐가 가득 차면 백프레셔로 대기한다."""
        while self._active and len(self._q) >= self._qmax:
            await asyncio.sleep(self.frame_ms / 1000.0 / 2)
        if not self._active:
            return
        self._q.append((self._submit_idx, frame))
        self._submit_idx += 1

    # ── 핵심: 한 스텝의 송신 결정 (순수 로직) ──────────────
    def _step(self, now_ms):
        """
        now_ms 시점에 보낼 frame 리스트와 다음 wake 시각을 반환한다.
        큐 상태(drop/소비)를 갱신한다. 반환: (frames_to_send, next_wake_ms|None)
        """
        out = []

        # 1) age > 2000ms : 반드시 폐기
        while self._q:
            idx = self._q[0][0]
            if now_ms - self._expected(idx) > BACKLOG_DISCARD_MS:
                self._q.popleft()
                self.stats.dropped_old += 1
            else:
                break

        # 2) backlog > 1600ms : 오래된 것부터 폐기
        while self._q:
            idx = self._q[0][0]
            if now_ms - self._expected(idx) > BACKLOG_PRESERVE_MS:
                self._q.popleft()
                self.stats.dropped_backlog += 1
            else:
                break

        if not self._q:
            return out, None

        # 3) head가 아직 due가 아니면 정상 pacing — 그 시각에 깨운다
        head_exp = self._expected(self._q[0][0])
        if head_exp > now_ms:
            return out, head_exp

        # 4) due — backlog에 따라 제한형 burst
        backlog_ms = now_ms - head_exp
        burst_ms = min(BURST_GROUP_MAX_MS, backlog_ms)
        burst_frames = max(1, int(burst_ms // self.frame_ms))
        burst_frames = min(burst_frames, self.max_burst_frames)

        for _ in range(burst_frames):
            if not self._q:
                break
            if self._expected(self._q[0][0]) > now_ms:   # 아직 due 아님
                break
            out.append(self._q.popleft()[1])

        next_wake = self._expected(self._q[0][0]) if self._q else None
        return out, next_wake

    # ── 메인 루프 ──────────────────────────────────────────
    async def run(self, producer_done=None):
        """
        페이싱 루프. producer_done(callable)이 True를 반환하고 큐가 비면
        자연 종료한다(3단계 로컬 파일의 1회 재생 종료에 사용).
        """
        while self._active:
            now = self._clock()
            frames, next_wake = self._step(now)
            if len(frames) >= 2:
                self.stats.bursts += 1
            for f in frames:
                await self._fanout(f)
                self.stats.sent += 1

            if not self._q:
                if producer_done is not None and producer_done():
                    log.info("[LIVE] source drained, pacing stops sent=%d",
                             self.stats.sent)
                    self._active = False
                    break
                await asyncio.sleep(self.frame_ms / 1000.0 / 4)
                continue

            if next_wake is None:
                await asyncio.sleep(0)
            else:
                delay = (next_wake - self._clock()) / 1000.0
                if delay > 0:
                    await asyncio.sleep(min(delay, self.frame_ms / 1000.0))

    async def stop(self):
        """LIVE_STOP: 남은 backlog frame은 즉시 폐기한다(07 정책)."""
        self._active = False
        discarded = len(self._q)
        self._q.clear()
        if discarded:
            log.info("[LIVE] pacing stop, discarded %d backlog frame(s)", discarded)
        log.info("[LIVE] pacing final stats %s", self.stats.snapshot())


class LiveRuntime:
    """
    페이싱 엔진 + 소스 producer + 태스크 수명 관리.
    LIVE 시작/정지의 런타임 객체. http_api가 이를 생성·보관한다.
    """

    def __init__(self, *, frame_ms, source, fanout, loop_source=False, queue_max=96):
        self.engine = PacingEngine(frame_ms, fanout, queue_max=queue_max)
        self.source = source
        self.loop_source = loop_source
        self._producer_done = False
        self._run_task = None
        self._prod_task = None

    async def start(self):
        self.engine.start()
        self._run_task = asyncio.create_task(
            self.engine.run(producer_done=lambda: self._producer_done))
        self._prod_task = asyncio.create_task(self._produce())

    async def _produce(self):
        try:
            async for frame in self.source.frames():
                if not self.engine.active:
                    break
                await self.engine.submit(frame)
        finally:
            self._producer_done = True

    async def stop(self):
        await self.engine.stop()
        for t in (self._prod_task, self._run_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def wait_done(self):
        """소스가 소진되어 자연 종료할 때까지 대기(테스트/1회 재생용)."""
        if self._run_task:
            await self._run_task

    def stats(self):
        return self.engine.stats.snapshot()