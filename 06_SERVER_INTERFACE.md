# 06. 서버 인터페이스

## 1. 목적

이 문서는 `P4/C6` 분리 구조에서 서버가 어떤 방식으로 C6에 명령을 전달하고,
C6가 어떤 형식으로 P4에 브리지하는지 정리한 정식 서버 인터페이스 사양이다.

대상 범위:

- WSS CMD 인터페이스
- WSS AUDIO 인터페이스
- HTTPS FILE 인터페이스
- 시험용 임시 서버 기준

## 2. 기본 연결 규격

### WSS CMD

- 용도: 제어 명령, 상태 보고, 파일/라이브 시작 요청
- 기본 URI: `wss://<host>:9001/cmd`
- 서브도메인 모드는 사용하지 않는다.

### WSS AUDIO

- 용도: 라이브 Opus 바이너리 프레임
- 기본 URI: `wss://<host>:9001/audio`
- 서브도메인 모드는 사용하지 않는다.

### HTTPS FILE

- 용도: 단일 파일 다운로드
- 기본 URI: `https://<host>:9002/<filename>`
- 호환 경로: `https://<host>:9002/file`
- 서브도메인 모드는 사용하지 않는다.

`9001`, `9002`는 개발/시험용 포트다. 운영에서는 현장 방화벽과 서버 구성에 맞춰 포트를 변경할 수 있다. 관공서/외부망 환경에서는 `443` 사용을 우선 검토한다.

운영 포트를 `443`으로 통합하는 경우 URI 예:

```text
CMD WSS   : wss://<host>/cmd
AUDIO WSS : wss://<host>/audio
HTTPS FILE: https://<host>/<filename>
```

포트를 변경하면 단말 설정, C6 설정, 서버 listen port, 방화벽/NAT, `FILE_START.https_url`을 모두 같은 기준으로 맞춰야 한다.

## 3. 세션 정책

- CMD WSS는 상시 유지가 기본이다.
- AUDIO WSS는 `LIVE_START` 이후에만 연결한다.
- FILE 다운로드는 HTTPS 단발 세션으로 처리한다.
- `LIVE`와 `FILE`는 동시 수행하지 않는다.
- FILE 진행 중 `LIVE_START`가 오면 FILE을 중단한다.

## 3-1. 지금 구현에서 가장 중요한 해석

- 서버는 `P4`가 아니라 `C6`와 대화한다.
- `P4`는 CMD WSS JSON을 직접 파싱하지 않는다.
- 서버가 보내는 JSON은 먼저 `C6 wss_stage2a.c`가 해석한다.
- 해석 결과가 `SDIO RPC` binary payload로 바뀌어 P4로 넘어간다.

## 4. CMD WSS JSON 명령

### 4.1 LIVE_START

필수/주요 필드:

- `type`: `"LIVE_START"`
- `ver`: 현재 시험 기준 `267`
- `cmd_id`
- `session_id`
- `frame_ms`
- `ready_timeout_sec`
- `record_flash`
- `file_name`

선택 필드:

- `sample_rate`
- `audio_url`
- `mode`
- `codec`

예:

```json
{
  "type": "LIVE_START",
  "ver": 267,
  "cmd_id": 1,
  "session_id": 1,
  "frame_ms": 40,
  "ready_timeout_sec": 30,
  "sample_rate": 16000,
  "mode": "LOW_LATENCY",
  "codec": "opus",
  "record_flash": 1,
  "file_name": "live.lopus"
}
```

처리 기준:

- `frame_ms == 0` 또는 비정상 값이면 `40`으로 보정
- `sample_rate == 0`이면 `16000`으로 보정
- `ready_timeout_sec`는 `1~60`, 범위 밖이면 `30`

### 4.2 LIVE_STOP

필드:

- `type`: `"LIVE_STOP"`
- `ver`
- `cmd_id`
- `session_id`
- `mode`
- `drain_timeout_ms`
- `fade_ms`

예:

```json
{
  "type": "LIVE_STOP",
  "ver": 267,
  "cmd_id": 2,
  "session_id": 1,
  "mode": "IMMEDIATE",
  "drain_timeout_ms": 2000,
  "fade_ms": 200
}
```

### 4.3 FILE_START

필드:

- `type`: `"FILE_START"`
- `ver`
- `cmd_id`
- `file_id`
- `https_url`
- `size`
- `sha256`
- `store_flash`
- `autoplay`
- `file_name`
- `resume_offset`

예:

```json
{
  "type": "FILE_START",
  "ver": 267,
  "cmd_id": 100,
  "file_id": 7,
  "https_url": "https://iotradio.co.kr:9002/voice001.mp3",
  "size": 1048576,
  "sha256": "<64 hex>",
  "store_flash": true,
  "autoplay": true,
  "file_name": "voice001.mp3",
  "resume_offset": 0
}
```

현재 구현 기준:

- `https_url` 또는 override URL이 반드시 있어야 한다.
- `size == 0`이면 중단한다.
- `resume_offset > total_size`이면 중단한다.
- P4는 현재 `resume_offset == 0`만 허용한다.

### 4.4 FILE_STOP

- `type` 문자열에 `FILE_STOP`이 포함되면 C6는 현재 다운로드를 중단한다.
- 중단 결과는 `FILE_ABORT(USER_CANCEL)`로 정리한다.

## 5. 서버 -> C6 -> P4 브리지 규격

### 라이브

- 서버 JSON `LIVE_START`
- C6 -> P4: `LIVE_CTRL_START(0x0130, 16B legacy / 80B with file_name)`
- P4 -> C6: `LIVE_CTRL_READY(0x0131, 8B payload)`
- C6 -> P4: `LIVE_FRAME(0x0401, 12B header + opus)`
- C6/P4 종료: `LIVE_CTRL_STOP(0x0132, 8B payload)`

### 파일

- 서버 JSON `FILE_START`
- C6 -> P4: `FILE_META(0x0500, 52B legacy / 116B with file_name)`
- C6 -> P4: `FILE_CHUNK(0x0501, 20B header + data)`
- C6 -> P4: `FILE_END notify(0x0502)`
- P4 -> C6: `FILE_END result(0x0502)` 또는 `FILE_ABORT(0x0503)`

## 5-1. SDIO 메시지 표

| 방향 | msg_type | 이름 | 설명 |
| --- | ---: | --- | --- |
| P4 -> C6 | `0x0103` | `WSS_HOST` | 서버 host/IP 전달 |
| P4 -> C6 | `0x0124` | `TIME_SET` | RTC epoch, rtc_valid, last_sync_age 전달 |
| C6 -> P4 | `0x0123` | `TIME_SYNC_RESULT` | SNTP 결과 전달 |
| C6 -> P4 | `0x0130` | `LIVE_CTRL_START` | 라이브 시작 준비 요청 |
| P4 -> C6 | `0x0131` | `LIVE_CTRL_READY` | 준비 성공/실패 응답 |
| C6 -> P4 | `0x0132` | `LIVE_CTRL_STOP` | 라이브 중지 지시 |
| C6 -> P4 | `0x0401` | `LIVE_FRAME` | Opus payload 전달 |
| P4 -> C6 | `0x0402` | `LIVE_STATS` | P4 buffer/underrun 통계 |
| C6 -> P4 | `0x0500` | `FILE_META` | 파일 메타 전달 |
| C6 -> P4 | `0x0501` | `FILE_CHUNK` | 파일 chunk 전달 |
| C6 -> P4 | `0x0502` | `FILE_END notify` | 다운로드 완료 통지 |
| P4 -> C6 | `0x0502` | `FILE_END result` | SHA256/저장 결과 응답 |
| P4 -> C6 | `0x0503` | `FILE_ABORT` | 파일 중단 응답 |

## 5-2. SDIO payload 크기

- 공통 최대 payload: `4096B`
- `LIVE_FRAME`: `12B header + payload`
- `FILE_META`: `52B` legacy, `116B` when `file_name[64]` is appended
- `FILE_CHUNK`: `20B header + payload`
- `FILE_END notify`: `12B`
- `LIVE_CTRL_START`: `16B` legacy, `80B` when `file_name[64]` is appended
- `LIVE_CTRL_READY`: `8B`

## 6. AUDIO WSS 데이터 규격

- AUDIO WSS는 binary frame만 유효하다.
- 현재 C6는 별도 WSS audio header를 기대하지 않고 `binary payload 자체를 Opus packet`으로 본다.
- 수신한 binary payload는 C6 내부 `seq`를 증가시키면서 P4에 `LIVE_FRAME`으로 전달한다.

즉, 현재 시험 서버는 `RTP 패킷`이나 `추가 헤더`가 아니라 `Opus packet bytes`를 그대로 보내는 쪽으로 이해하면 된다.

서버가 보내면 안 되는 데이터:

- `.opus` 파일 전체
- Ogg page 전체
- `OggS`로 시작하는 Ogg container bytes
- `OpusHead`
- `OpusTags`
- 여러 Opus packet을 하나의 WSS binary payload에 합친 데이터

Ogg Opus 파일을 소스로 사용할 경우 서버가 Ogg page를 풀어 audio Opus packet만 추출해야 한다. 현재 테스트 서버는 `OpusHead`, `OpusTags`를 건너뛰고 실제 audio packet만 전송한다.

### 선택형 LFRM wrapper

C6는 선택적으로 `LFRM` wrapper도 해석할 수 있다. 단, 1차 서버 데모에서는 사용하지 않는 것을 권장한다.

```text
offset  size  의미
0       4     ASCII "LFRM"
4       8     reserved, 현재 C6 코드에서는 사용하지 않음
12      2     frame_ms, big-endian
14      2     opus_payload_len, big-endian
16      N     opus_payload
```

`LFRM`을 쓰면 C6는 wrapper를 제거하고 `opus_payload`만 P4에 전달한다.

현재 SDIO LIVE_FRAME payload:

- `session_id` 4B
- `seq` 4B
- `frame_ms` 2B
- `payload_len` 2B
- `opus_payload` N

## 7. HTTPS 파일 규격

- C6는 `esp_http_client`로 `https_url`에 접속한다.
- read buffer는 `2048B`
- SDIO 전송은 `FILE_CHUNK` 형식으로 분할한다.
- `sha256` 최종 검증 권한자는 P4다.

현재 구현 해석:

- `C6`는 다운로드 브리지다.
- `P4`가 최종 검증자다.
- 서버는 `FILE_END` 또는 `FILE_ABORT` JSON으로만 최종 결과를 받는다.

현재 P4 파일 정책:

- 최대 `4MB`
- 전체 수신 후 SHA256 검증
- 성공 시 autoplay 또는 flash 저장
- 자동 재생 경로는 현재 MP3에 맞춰 운용하는 것이 안전

## 8. 실패 응답 규격

### FILE_ABORT reason

- `0x01`: STORAGE_FAIL
- `0x02`: PREEMPTED_BY_LIVE
- `0x03`: CREDIT_TIMEOUT
- `0x04`: NET_ERROR
- `0x05`: BAD_FIELD
- `0x06`: USER_CANCEL
- `0x07`: NO_PSRAM

### FILE_END result

- `0x00`: OK
- `0x01`: SHA256_FAIL
- `0x02`: STORAGE_FAIL
- `0x03`: BAD_FIELD
- `0x04`: NO_PSRAM

### WSS reason

- `0x0001`: disconnect
- `0x0002`: error
- `0x0003`: timeout
- `0x0004`: echo timeout

## 9. 시험 기준 서버

시험 기준 서버는 `D:\p4\examples\ESP-IDF\iot_radio\test_server`를 사용한다.

### 권장 스크립트

- `iot_radio_test_server.py`

역할:

- WSS CMD 서버
- WSS AUDIO 서버
- HTTPS 파일 서버
- 콘솔에서 `LIVE_START`, `FILE_START` 송신

## 9-1. 현재 가장 권장하는 테스트 방법

### 준비

1. PC와 장비가 같은 네트워크에 있어야 한다.
2. PC IP를 확인한다. 예: `192.168.0.5`
3. 장비에 저장된 서버 IP도 같은 값이어야 한다.
4. `cert.pem`과 C6의 `wss_server_cert.pem`이 같은 인증서여야 한다.

### 실행

```powershell
cd D:\p4\examples\ESP-IDF\iot_radio\test_server
python .\iot_radio_test_server.py
```

기본값 변경이 필요하면 아래 옵션을 사용한다.

- `--record-flash` 또는 `--live-record-flash`: 기본 LIVE 전송의 `record_flash=1`
- `--no-live-record-flash`: 기본 LIVE 전송의 `record_flash=0`
- `--file-store-flash`: 기본 FILE 전송의 `store_flash=1`
- `--no-file-store-flash`: 기본 FILE 전송의 `store_flash=0`

### 콘솔 조작

- `1` 또는 `live`: 기본값으로 LIVE 시험
- `1s` 또는 `live-save`: `record_flash=1`로 LIVE 시험
- `1n` 또는 `live-nosave`: `record_flash=0`으로 LIVE 시험
- `2` 또는 `file`: 기본값으로 FILE 시험
- `2s` 또는 `file-save`: `store_flash=1`로 FILE 시험
- `2n` 또는 `file-nosave`: `store_flash=0`으로 FILE 시험
- `q`: 종료

### LIVE 시험에서 기대하는 흐름

1. CMD WSS 연결
2. `LIVE_START` 전송
3. C6가 P4에 `LIVE_CTRL_START`
4. P4가 `LIVE_CTRL_READY status=0`
5. AUDIO WSS 연결
6. Opus binary frame 송신
7. 장비에서 실시간 재생
8. 끝나면 `LIVE_STOP`

### FILE 시험에서 기대하는 흐름

1. CMD WSS 연결
2. `FILE_START` 전송
3. C6가 HTTPS로 `voice001.mp3` 읽기
4. P4가 chunk 수신
5. SHA256 검증
6. autoplay 또는 flash save
7. 서버가 `FILE_END verify_ok=true` 수신

## 9-2. 로그 체크 포인트

### 서버 콘솔

- `[CMD] connected`
- `[LIVE] start ...`
- `[AUDIO] start stream ...`
- `[FILE] start url=...`
- `[CMD] stats {...}` 또는 종료 결과

### C6 로그

- `[SNTP] synced`
- `[WSS] connected`
- `[LIVE] ready status=0`
- `[FILE] end result ... result=0x00`

### P4 로그

- `NET_STATE wifi=1 wss=1 ...`
- `LIVE_CTRL_START ...`
- `[CMD] FILE_START ...`
- `[CMD] FILE_END_NOTIFY ...`
- `file saved path=...` 또는 autoplay 재생

## 9-3. 실패 시 바로 보는 체크리스트

### WSS가 안 붙는 경우

- 서버 host가 실제 서버를 가리키는지
- 시험 포트 `9001` 또는 운영 포트 `443`이 열려 있는지
- 인증서가 같은지
- C6 시간이 맞는지

### FILE가 실패하는 경우

- `https_url`가 실제 서버를 가리키는지
- 시험 포트 `9002` 또는 운영 포트 `443`이 열려 있는지
- 파일 크기와 SHA256이 맞는지
- MP3 파일을 쓰고 있는지

### LIVE가 시작만 되고 소리가 안 나는 경우

- `LIVE_CTRL_READY status=0`가 나왔는지
- AUDIO WSS가 연결됐는지
- `voice001_mono_40ms.opus` 파일을 쓰는지
- P4 쪽 `LIVE_FRAME` 수신 로그가 나오는지

## 10. 인증서 기준

- 서버 인증서: `D:\p4\examples\ESP-IDF\iot_radio\test_server\cert.pem`
- C6 내장 인증서: `D:\p4\slave\main\wss_server_cert.pem`

두 파일은 동일해야 하며, 현재 기준으로 동일 인증서다.

## 11. 결론

이 프로젝트의 서버 인터페이스는 `CMD WSS + AUDIO WSS + HTTPS FILE + SDIO RPC 브리지` 구조로 확정하는 것이 맞다.
앞으로 서버 쪽 기능을 늘릴 때도 이 경계를 유지해야 P4 메모리 보호 원칙이 깨지지 않는다.
