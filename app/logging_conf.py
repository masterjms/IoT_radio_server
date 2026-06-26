"""
logging_conf.py — 표준 로그 포맷.

사양 문서의 로그 컨벤션을 따라 [태그] 형태의 접두를 사용한다.
예: [WSS] connected, [LIVE] start, [FILE] end result=0x00
journald가 수집하므로 stdout으로만 출력한다.
"""

import logging
import sys


def setup_logging(level=logging.INFO):
    """루트 로거를 stdout 출력으로 구성한다."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    return root


def get_logger(name):
    return logging.getLogger(name)