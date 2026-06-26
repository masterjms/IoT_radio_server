"""
test_pacing.py — 07 정책 페이싱 엔진 회귀 테스트.

시계를 주입해 시간을 제어하면서 backlog/burst/drop을 검증한다.
실행: PYTHONPATH=app python3 tests/test_pacing.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from pacing import PacingEngine  # noqa: E402

FM = 40


def _engine():
    async def fanout(f):
        pass
    eng = PacingEngine(FM, fanout, clock=lambda: 0)
    eng._start_ms = 0
    eng._active = True
    return eng


def _load(eng, n, start_idx=0):
    for i in range(n):
        eng._q.append((start_idx + i, bytes([(start_idx + i) & 0xFF])))
    eng._submit_idx = start_idx + n


def test_normal_pacing():
    eng = _engine()
    _load(eng, 5)
    frames, wake = eng._step(0)
    assert len(frames) == 1 and wake == 40
    frames, wake = eng._step(40)
    assert len(frames) == 1 and wake == 80


def test_future_frame_held():
    eng = _engine()
    _load(eng, 5)
    eng._step(20)                # index0 소비
    frames, wake = eng._step(20) # head=index1 exp=40 > 20
    assert frames == [] and wake == 40


def test_burst_capped_120ms():
    eng = _engine()
    _load(eng, 10)
    frames, _ = eng._step(160)   # backlog 160ms
    assert len(frames) == 3      # 120ms cap = 3 frame


def test_backlog_drop_1600():
    eng = _engine()
    _load(eng, 50)
    eng._step(1700)
    assert eng.stats.dropped_backlog == 3


def test_age_drop_2000():
    eng = _engine()
    _load(eng, 100)
    eng._step(3000)
    assert eng.stats.dropped_old == 25


def test_empty_queue():
    eng = _engine()
    frames, wake = eng._step(1000)
    assert frames == [] and wake is None


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("\n%d passed" % len(tests))


if __name__ == "__main__":
    run_all()