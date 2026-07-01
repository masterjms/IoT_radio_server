"""
logging_conf.py — 표준 로그 포맷.

사양 문서의 로그 컨벤션을 따라 [태그] 형태의 접두를 사용한다.
예: [WSS] connected, [LIVE] start, [FILE] end result=0x00
journald가 수집하므로 stdout으로만 출력한다.
"""

import logging
import sys

from aiohttp.abc import AbstractAccessLogger

# access 로그에서 제외할 경로.
QUIET_PATHS = {"/api/health"}


class QuietAccessLogger(AbstractAccessLogger):
    """기본 access logger와 동일하되, QUIET_PATHS는 기록하지 않는다."""

    def log(self, request, response, time):
        if request.path in QUIET_PATHS:
            return
        self.logger.info(
            '%s [%s] "%s %s HTTP/%s.%s" %s %s "%s" "%s"',
            request.remote,
            self._now_str(),
            request.method,
            request.path_qs,
            request.version.major,
            request.version.minor,
            response.status,
            response.body_length,
            request.headers.get("Referer", "-"),
            request.headers.get("User-Agent", "-"),
        )

    @staticmethod
    def _now_str():
        import datetime
        return datetime.datetime.now().strftime("%d/%b/%Y:%H:%M:%S %z")


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