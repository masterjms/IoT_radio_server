"""
file_manager.py — 업로드 파일 저장, SHA256 계산, 메타 관리.

군포 앱이 /upload로 보낸 MP3를 media/uploads/에 저장하고,
FILE_START에 들어갈 size, sha256, https_url, file_name을 준비한다.

저장 파일명 정책:
 - 원본 파일명을 그대로 쓰지 않는다(경로 조작·충돌 방지).
 - 서버가 file-<epoch>.<ext> 형태로 새 이름을 부여한다.
 - 확장자는 화이트리스트(mp3)만 허용한다. 사양상 자동 재생은 MP3 중심.

https_url은 nginx가 정적 서빙하는 /files/ 경로로 만든다.
 예: https://iotradio.co.kr/files/file-1779067243.mp3
"""

import hashlib
import os
import time

import config
from logging_conf import get_logger

log = get_logger("file_mgr")

ALLOWED_EXT = {".mp3"}
_CHUNK = 64 * 1024


class FileTooLarge(Exception):
    pass


class BadFileType(Exception):
    pass


class FileManager:
    def __init__(self):
        self._index = {}   # file_name -> meta dict
        # 재시작 시 기존 업로드를 메모리 인덱스로 복구
        self._scan_existing()

    def _scan_existing(self):
        if not os.path.isdir(config.UPLOAD_DIR):
            return
        for name in os.listdir(config.UPLOAD_DIR):
            path = os.path.join(config.UPLOAD_DIR, name)
            if os.path.isfile(path):
                try:
                    self._index[name] = self._build_meta(name, path)
                except Exception as e:
                    log.warning("[FILE] scan skip %s: %s", name, e)
        if self._index:
            log.info("[FILE] recovered %d existing upload(s)", len(self._index))

    def _build_meta(self, file_name, path):
        size = os.path.getsize(path)
        sha = self._sha256_of(path)
        return {
            "file_name": file_name,
            "size": size,
            "sha256": sha,
            "https_url": self._url_for(file_name),
            "path": path,
        }

    @staticmethod
    def _sha256_of(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                b = f.read(_CHUNK)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    @staticmethod
    def _url_for(file_name):
        return f"https://{config.PUBLIC_DOMAIN}{config.FILES_URL_PREFIX}/{file_name}"

    @staticmethod
    def _check_ext(original_name):
        ext = os.path.splitext(original_name or "")[1].lower()
        if ext not in ALLOWED_EXT:
            raise BadFileType(f"unsupported extension: {ext or '(none)'}")
        return ext

    async def save_stream(self, original_name, field):
        """
        aiohttp multipart 필드를 스트리밍으로 디스크에 저장하면서
        동시에 SHA256과 크기를 계산한다. 4MB 초과 시 중단·삭제.
        반환: 메타 dict
        """
        ext = self._check_ext(original_name)
        file_name = f"file-{int(time.time())}{ext}"
        path = os.path.join(config.UPLOAD_DIR, file_name)

        h = hashlib.sha256()
        size = 0
        try:
            with open(path, "wb") as out:
                while True:
                    chunk = await field.read_chunk(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > config.FILE_MAX_BYTES:
                        raise FileTooLarge(
                            f"exceeds {config.FILE_MAX_BYTES} bytes")
                    h.update(chunk)
                    out.write(chunk)
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            raise

        meta = {
            "file_name": file_name,
            "size": size,
            "sha256": h.hexdigest(),
            "https_url": self._url_for(file_name),
            "path": path,
        }
        self._index[file_name] = meta
        log.info("[FILE] saved name=%s size=%d sha256=%s",
                 file_name, size, meta["sha256"][:12])
        return meta

    def get(self, file_name):
        return self._index.get(file_name)

    def list_files(self):
        return [
            {k: m[k] for k in ("file_name", "size", "sha256", "https_url")}
            for m in self._index.values()
        ]