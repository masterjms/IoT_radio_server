"""
opus_source.py — Opus 프레임 소스.

3단계 라이브 검증용. 서버 로컬의 Ogg Opus(.opus) 파일에서
audio Opus packet만 추출한다.

사양(06/00 문서)이 강하게 요구하는 규칙:
 - /audio WSS로 보내는 것은 header 없는 순수 Opus packet이다.
 - Ogg page 전체, OpusHead, OpusTags를 보내면 안 된다.
 - 따라서 Ogg 컨테이너를 풀어 OpusHead/OpusTags를 건너뛰고
   실제 audio packet만 추출해야 한다.
 - WSS binary 1개 = Opus packet 1개. 절대 합치지 않는다.

4단계에서는 이 소스가 /ingest(군포 앱)로 대체된다. 페이싱 엔진은
소스가 무엇이든 동일하게 동작한다.
"""

import asyncio

from logging_conf import get_logger

log = get_logger("opus_src")


def iter_ogg_opus_packets(path):
    """
    Ogg 컨테이너를 파싱해 Opus packet을 순서대로 yield 한다.
    OpusHead/OpusTags(헤더 패킷)는 제외하고 audio packet만 내보낸다.

    Ogg page 구조:
      "OggS"(4) ver(1) htype(1) granule(8) serial(4) seq(4) crc(4)
      nsegs(1) segment_table(nsegs) body(...)
    packet은 segment를 이어붙이되, 길이 255 미만 segment에서 끝난다.
    255인 segment는 다음 segment(또는 다음 page)로 packet이 이어진다.
    """
    with open(path, "rb") as f:
        data = f.read()

    pos = 0
    carry = b""        # page 경계를 넘어 이어지는 packet 누적분
    n = len(data)

    while pos < n:
        if data[pos:pos + 4] != b"OggS":
            nxt = data.find(b"OggS", pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue

        nsegs = data[pos + 26]
        seg_table = data[pos + 27:pos + 27 + nsegs]
        body = pos + 27 + nsegs

        idx = body
        cur = carry
        for seg_len in seg_table:
            cur += data[idx:idx + seg_len]
            idx += seg_len
            if seg_len < 255:
                # packet 완성
                if not (cur.startswith(b"OpusHead") or cur.startswith(b"OpusTags")):
                    yield cur
                cur = b""
        carry = cur    # 255로 끝났으면 다음 page로 이어짐
        pos = body + sum(seg_table)


class LocalOpusSource:
    """
    .opus 파일에서 프레임을 비동기로 공급하는 소스.
    `loop=True`면 파일 끝에서 처음으로 되감아 무한 재생한다.
    """

    def __init__(self, path, *, loop=False):
        self.path = path
        self.loop = loop
        self._packets = list(iter_ogg_opus_packets(path))
        log.info("[LIVE] source loaded path=%s packets=%d loop=%s",
                 path, len(self._packets), loop)

    @property
    def packet_count(self):
        return len(self._packets)

    async def frames(self):
        """audio packet을 순서대로 비동기 yield. 페이싱이 throttle하므로 즉시 내보낸다."""
        while True:
            for pkt in self._packets:
                yield pkt
            if not self.loop:
                return
            await asyncio.sleep(0)   # 무한 루프 양보