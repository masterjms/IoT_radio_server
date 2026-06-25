# 08. 방송 흐름 예제

## 1. 목적

이 문서는 실제 동작을 예제로 따라가기 위한 문서다.
정식 필드 규격은 `06_SERVER_INTERFACE.md`, 전체 상태 흐름은 `03_RUNTIME_FLOW.md`를 기준으로 하고,
여기서는 `서버 -> C6 -> P4 -> 내부 처리` 순서로 어떤 일이 일어나는지 설명한다.

핵심 관점은 다음과 같다.

- 서버는 항상 `C6`와 통신한다.
- C6는 서버 JSON과 HTTPS/WSS payload를 해석하거나 받아서 `SDIO RPC`로 P4에 넘긴다.
- P4는 네트워크를 직접 처리하지 않고 재생, 버퍼링, 저장, 검증을 담당한다.
- 내부 처리는 P4의 `sdio_rpc`, `live_ctrl_task`, `live_audio_task`, `file_rx`, `live_record` 기준으로 이해한다.

## 2. 공통 준비 상태

방송 예제는 아래 상태를 전제로 한다.

1. P4가 부팅된다.
2. P4가 SDIO/ESP-Hosted를 초기화한다.
3. C6가 Wi-Fi STA에 연결한다.
4. C6가 SNTP 또는 RTC 기반 시간 조건을 만족한다.
5. C6가 서버의 CMD WSS에 연결한다.
   - 시험 포트: `wss://<host>:9001/cmd`
   - 운영 443 포트: `wss://<host>/cmd`
6. P4는 C6로부터 `NET_STATE wifi=1 wss=1 ip!=0` 상태를 받는다.

이 상태가 되면 서버는 `LIVE_START` 또는 `FILE_START`를 보낼 수 있다.

## 3. FILE 정상 예제

### 3.1 서버 명령

서버는 CMD WSS로 `FILE_START` JSON을 보낸다.

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

### 3.2 서버에서 C6까지

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | 서버 | CMD WSS `/cmd`로 `FILE_START` 전송 |
| 2 | C6 | `wss_stage2a.c`가 JSON에서 `cmd_id`, `file_id`, `size`, `https_url`, `sha256`, `store_flash`, `autoplay`, `file_name` 파싱 |
| 3 | C6 | LIVE가 진행 중이면 파일을 시작하지 않고 `PREEMPTED_BY_LIVE`로 중단 |
| 4 | C6 | 필드가 정상이고 LIVE가 없으면 파일 다운로드 컨텍스트 생성 |
| 5 | C6 | P4에 `FILE_META(0x0500, 52B legacy / 116B with file_name)` 전송 |

### 3.3 C6에서 P4까지

`FILE_META` payload는 아래 의미를 가진다.

| offset | 필드 | 의미 |
| ---: | --- | --- |
| 0 | `cmd_id` | 서버 명령 ID |
| 4 | `file_id` | 파일 ID |
| 8 | `total_size` | 전체 파일 크기 |
| 12 | `resume_offset` | 현재 P4 구현은 `0`만 허용 |
| 16 | `sha256_raw[32]` | 최종 검증용 SHA256 |
| 48 | `store_flash` | 성공 후 LittleFS 저장 여부 |
| 49 | `autoplay` | 성공 후 즉시 재생 여부 |
| 52 | `file_name[64]` | 선택 필드. 서버가 지정한 저장 파일명 |

P4의 `sdio_rpc.c`는 `FILE_META`를 받아 `file_rx_on_meta()`로 넘긴다.

### 3.4 P4 내부 처리

| 단계 | 내부 모듈 | 처리 |
| ---: | --- | --- |
| 1 | `sdio_rpc.c` | `FILE_META` payload 길이와 필드를 파싱 |
| 2 | `file_rx.c` | `total_size`가 `0`이거나 4MB 초과인지 검사 |
| 3 | `file_rx.c` | `resume_offset != 0`이면 현재 구현에서는 중단 |
| 4 | `file_rx.c` | PSRAM에 전체 파일 크기만큼 수신 버퍼 확보 |
| 5 | `file_rx.c` | 앱 상태 이벤트 `FILE_DOWNLOAD_START` 발생 |
| 6 | C6 | HTTPS로 `https_url`을 열고 2048B 단위로 읽음 |
| 7 | C6 | 읽은 데이터를 `FILE_CHUNK(0x0501)`로 P4에 반복 전송 |
| 8 | P4 `file_rx.c` | chunk offset 기준으로 PSRAM 버퍼에 복사 |
| 9 | C6 | 다운로드 완료 후 `FILE_END notify(0x0502)` 전송 |
| 10 | P4 `file_rx.c` | `last_offset == total_size` 확인 후 SHA256 계산 |
| 11 | P4 `file_rx.c` | SHA256 일치 시 `FILE_END result=0x00`을 C6에 응답 |
| 12 | P4 `file_rx.c` | `store_flash=true`이면 `/spiffs/rec/<file_name>-W.<ext>` 저장. `file_name`에 `-<epoch>`가 없으면 저장 시점 epoch를 붙이고, `file_name`이 없으면 `file-<epoch>-W.mp3`로 저장 |
| 13 | P4 `file_rx.c` | `autoplay=true`이면 PSRAM 버퍼에서 바로 재생 |
| 14 | P4 `file_rx.c` | 재생/저장 후 PSRAM 버퍼 정리 |

### 3.5 C6에서 서버로 결과 보고

P4가 `FILE_END result`를 보내면 C6는 서버에 최종 결과 JSON을 보낸다.

성공 예:

```json
{
  "type": "FILE_END",
  "ver": 267,
  "cmd_id": 100,
  "file_id": 7,
  "verify_ok": true
}
```

실패 예:

```json
{
  "type": "FILE_END",
  "ver": 267,
  "cmd_id": 100,
  "file_id": 7,
  "verify_ok": false,
  "fail_reason": "SHA256_FAIL"
}
```

## 4. LIVE 정상 예제

### 4.1 서버 명령

서버는 CMD WSS로 `LIVE_START` JSON을 보낸다.

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
  "record_flash": 0,
  "file_name": "live.lopus"
}
```

`record_flash=1`이면 실시간 방송을 수신하면서 Opus를 기록하고, 종료 후 `file_name` 기준으로 저장한다. `file_name`에 `-<epoch>`가 없으면 P4가 저장 시점 epoch를 붙이고, 저장명에는 `-W`를 붙인다. 예: `live.lopus` -> `/spiffs/rec/live-<epoch>-W.lopus`

### 4.2 서버에서 C6까지

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | 서버 | CMD WSS `/cmd`로 `LIVE_START` 전송 |
| 2 | C6 | `session_id`, `frame_ms`, `sample_rate`, `ready_timeout_sec`, `record_flash`, `file_name` 파싱 |
| 3 | C6 | `frame_ms == 0` 또는 비정상 값이면 `40`으로 보정 |
| 4 | C6 | `sample_rate == 0`이면 `16000`으로 보정 |
| 5 | C6 | `ready_timeout_sec`가 `1~60` 범위 밖이면 `30`으로 보정 |
| 6 | C6 | 파일 다운로드 중이면 `FILE_ABORT(PREEMPTED_BY_LIVE)` 처리 |
| 7 | C6 | P4에 `LIVE_CTRL_START(0x0130, 16B legacy / 80B with file_name)` 전송 |
| 8 | C6 | `s_wait_p4_ready=true`로 두고 P4 READY를 기다림 |

### 4.3 P4 준비 처리

| 단계 | 내부 모듈 | 처리 |
| ---: | --- | --- |
| 1 | `sdio_rpc.c` | `LIVE_CTRL_START` payload 파싱 |
| 2 | `sdio_rpc.c` | live frame 통계와 큐를 초기화 |
| 3 | `live_ctrl_task.c` | `PREPARING` 상태 진입 |
| 4 | `live_ctrl_task.c` | ready timeout 타이머 시작 |
| 5 | `live_ctrl_task.c` | `live_audio_notify_start()` 호출 |
| 6 | `live_audio_task.c` | packet queue, jitter, Opus decode, PCM queue, I2S 출력 태스크 준비 |
| 7 | `live_ctrl_task.c` | `record_flash=1`이면 `live_record_start()`로 저장 버퍼 준비 |
| 8 | `live_ctrl_task.c` | 준비 성공 시 `LIVE_CTRL_READY status=0`을 C6에 전송 |

현재 P4 준비 루틴은 대부분 동기식으로 빠르게 끝난다.
따라서 `ready_timeout_sec=30`은 일반적인 오디오 스트림 대기 시간이 아니라,
P4가 `LIVE_CTRL_READY`를 못 보내고 PREPARING에 묶이는 상황을 막는 준비 제한 시간이다.

### 4.4 C6가 AUDIO WSS를 여는 시점

P4가 `LIVE_CTRL_READY status=0`을 보내면 C6는 아래처럼 동작한다.

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | C6 | `sdio_rpc_slave.c`가 P4의 `LIVE_CTRL_READY` 수신 |
| 2 | C6 | `wss_stage2a_on_live_ready()`에서 `s_p4_ready_ok=true` 설정 |
| 3 | C6 | 메인 WSS loop에서 `s_wait_p4_ready=false` 처리 |
| 4 | C6 | `audio_wss_start()` 호출 |
| 5 | C6 | 서버의 AUDIO WSS `/audio`에 연결 |
| 6 | 서버 | `/audio` 연결을 확인하고 Opus binary frame 송신 시작 |

중요한 점은 C6가 P4 READY 전에 `/audio`를 열지 않는다는 것이다.
이렇게 해야 P4 준비 전 초반 Opus frame이 C6에서 drop되는 문제를 줄일 수 있다.

### 4.5 LIVE frame 재생 처리

서버는 AUDIO WSS binary message 하나에 Opus frame 하나를 보낸다.

| 단계 | 내부 모듈 | 처리 |
| ---: | --- | --- |
| 1 | 서버 | `frame_ms` 간격으로 Opus binary frame 송신 |
| 2 | C6 | AUDIO WSS event에서 binary frame 수신 |
| 3 | C6 | payload 길이가 4000B 초과이면 drop |
| 4 | C6 | `seq` 증가 후 P4에 `LIVE_FRAME(0x0401)` 전송 |
| 5 | P4 `sdio_rpc.c` | `LIVE_FRAME`을 수신해 payload를 PSRAM 또는 내부 메모리에 복사 |
| 6 | P4 `sdio_rpc.c` | SDIO callback에서 직접 무거운 처리하지 않고 live frame worker queue로 넘김 |
| 7 | P4 worker | `live_record_append()`로 저장 대상이면 Opus 기록 |
| 8 | P4 worker | `live_audio_enqueue_packet()`으로 audio packet queue에 삽입 |
| 9 | P4 `live_audio_task.c` | jitter queue에서 순서 보정과 최소 버퍼링 수행 |
| 10 | P4 `live_audio_task.c` | Opus decode 후 PCM queue에 적재 |
| 11 | P4 `live_audio_task.c` | PCM prefill 후 I2S로 출력 |
| 12 | P4 `live_audio_task.c` | `LIVE_STATS`로 buffer, underrun, seq 상태를 C6에 보고 가능 |

현재 기본 버퍼 기준은 아래와 같다.

| 항목 | 값 | 40 ms frame 기준 |
| --- | ---: | ---: |
| 시작 jitter buffer | 20 frame | 800 ms |
| rebuffer 기준 | 8 frame | 320 ms |
| jitter slot 최대 | 48 frame | 1920 ms |
| PCM prefill | 8 frame | 320 ms |
| PCM low-water | 1 frame | 40 ms |

## 5. LIVE 정상 종료 예제

### 5.1 서버 명령

서버는 CMD WSS로 `LIVE_STOP` JSON을 보낸다.

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

### 5.2 종료 흐름

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | 서버 | CMD WSS로 `LIVE_STOP` 전송 |
| 2 | C6 | `s_audio_stop_req=true` 설정 |
| 3 | C6 | P4에 `LIVE_CTRL_STOP(0x0132)` 전송 |
| 4 | C6 | AUDIO WSS를 닫음 |
| 5 | P4 `sdio_rpc.c` | `LIVE_CTRL_STOP` 수신 |
| 6 | P4 `live_ctrl_task.c` | 정상 종료 reason이면 tail wait 후 teardown |
| 7 | P4 `live_audio_task.c` | PCM/I2S 출력 태스크 정리 |
| 8 | P4 `live_record.c` | `record_flash=1`이면 `file_name` 기준 `/spiffs/rec/<name>-<epoch>-W.<ext>` 저장 |
| 9 | P4 | 앱 상태를 READY 또는 IDLE로 복귀 |

## 6. ERROR 예제

### 6.1 FILE_BAD_FIELD

대표 조건은 다음과 같다.

- `size == 0`
- `https_url` 없음
- `resume_offset != 0`
- chunk offset이 전체 크기를 초과

흐름:

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | 서버 | 잘못된 `FILE_START` 전송 |
| 2 | C6 | 필드가 명백히 잘못되면 P4로 넘기기 전 `FILE_ABORT BAD_FIELD`를 서버에 보고 |
| 3 | P4 | `FILE_META`까지 도달한 뒤 `resume_offset != 0` 등을 발견하면 `FILE_ABORT BAD_FIELD`를 C6에 응답 |
| 4 | C6 | 서버에 `FILE_ABORT fail_reason=BAD_FIELD` 보고 |

예:

```json
{
  "type": "FILE_ABORT",
  "ver": 267,
  "cmd_id": 100,
  "file_id": 7,
  "last_offset": 0,
  "reason": 5,
  "fail_reason": "BAD_FIELD"
}
```

### 6.2 FILE_NO_PSRAM

P4가 파일 전체 크기만큼 PSRAM 버퍼를 확보하지 못하면 발생한다.

흐름:

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | C6 | 정상 `FILE_META`를 P4에 전송 |
| 2 | P4 `file_rx.c` | `heap_caps_malloc(total_size, MALLOC_CAP_SPIRAM)` 실패 |
| 3 | P4 | C6에 `FILE_ABORT NO_PSRAM` 전송 |
| 4 | C6 | HTTPS 다운로드를 중단하고 서버에 `FILE_ABORT NO_PSRAM` 보고 |

### 6.3 FILE_SHA256_FAIL

파일 수신은 완료됐지만 P4가 계산한 SHA256이 서버가 보낸 값과 다를 때 발생한다.

흐름:

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | C6 | HTTPS 파일을 끝까지 읽고 P4에 모든 chunk 전송 |
| 2 | C6 | P4에 `FILE_END notify` 전송 |
| 3 | P4 `file_rx.c` | 전체 PSRAM 버퍼 SHA256 계산 |
| 4 | P4 | SHA256 불일치 시 `FILE_END result=0x01` 응답 |
| 5 | C6 | 서버에 `FILE_END verify_ok=false fail_reason=SHA256_FAIL` 보고 |
| 6 | P4 | autoplay/store_flash 수행하지 않고 수신 버퍼 정리 |

### 6.4 FILE_PREEMPTED_BY_LIVE

파일 다운로드 중 LIVE가 들어오면 LIVE가 우선이다.

흐름:

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | 서버 | FILE 진행 중 `LIVE_START` 전송 |
| 2 | C6 | `s_file_ctx.active`이면 `file_request_abort(PREEMPTED_BY_LIVE)` 처리 |
| 3 | C6 | P4에 `FILE_ABORT PREEMPTED_BY_LIVE` 전송 |
| 4 | C6 | 서버에 `FILE_ABORT PREEMPTED_BY_LIVE` 보고 |
| 5 | C6 | 이후 LIVE 준비 절차 진행 |
| 6 | P4 | 파일 수신 버퍼 정리 후 LIVE 준비/재생으로 전환 |

### 6.5 LIVE_P4_READY_FAIL_OR_TIMEOUT

P4가 LIVE 준비에 실패하거나 준비 제한 시간 안에 READY를 못 보내면 발생한다.

흐름:

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | 서버 | `LIVE_START ready_timeout_sec=30` 전송 |
| 2 | C6 | P4에 `LIVE_CTRL_START` 전송 후 `s_wait_p4_ready=true` |
| 3 | P4 | PREPARING 상태에서 준비 실패 또는 timeout 발생 |
| 4 | P4 | C6에 `LIVE_CTRL_READY status=1/2/3` 전송 |
| 5 | C6 | `s_p4_ready_fail=true`, `s_live_active=false` 처리 |
| 6 | C6 | AUDIO WSS를 열지 않음 |

현재 구현에서는 C6가 `LIVE_READY` 결과 JSON을 서버에 별도로 보내지 않는다.
서버는 C6가 `/audio`에 접속하지 않는 것으로 간접 확인하게 된다.
추후 개선 시 C6가 서버에 아래와 같은 명시 결과를 보내는 것이 좋다.

```json
{
  "type": "LIVE_READY",
  "ver": 267,
  "session_id": 1,
  "status": 1,
  "reason": 1
}
```

### 6.6 LIVE_AUDIO_STALL

AUDIO WSS가 열렸지만 일정 시간 동안 오디오 frame이 들어오지 않으면 C6는 stall로 판단한다.

흐름:

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | P4 | READY 성공 |
| 2 | C6 | AUDIO WSS 연결 |
| 3 | 서버 | 오디오 frame 송신이 멈춤 |
| 4 | C6 | `s_audio_last_rx_tick` 기준 `WSS_AUDIO_STALL_MS` 초과 감지 |
| 5 | C6 | `live_ctrl_stop_if_needed(WSS_REASON_TIMEOUT)` 호출 |
| 6 | C6 | P4에 `LIVE_CTRL_STOP reason=0x0003` 전송 |
| 7 | P4 | live audio teardown 및 상태 복귀 |

### 6.7 LIVE_AUDIO_DISCONNECT

AUDIO WSS가 라이브 중 끊기면 C6는 disconnect reason으로 LIVE를 정리한다.

흐름:

| 단계 | 주체 | 처리 |
| ---: | --- | --- |
| 1 | 서버 또는 네트워크 | AUDIO WSS 연결 끊김 |
| 2 | C6 | `WEBSOCKET_EVENT_DISCONNECTED` 수신 |
| 3 | C6 | live 진행 중이면 `live_ctrl_stop_if_needed(WSS_REASON_DISCONNECT)` 호출 |
| 4 | C6 | P4에 `LIVE_CTRL_STOP reason=0x0001` 전송 |
| 5 | P4 | live audio teardown, record flush, 상태 복귀 |

## 7. 로그 확인 예제

### 7.1 FILE 정상 로그 흐름

```text
C6: [FILE] start cmd_id=... file_id=... size=... store=1 autoplay=1
P4: [CMD] FILE_START cmd_id=... file_id=... size=... store=1 autoplay=1
P4: [CMD] FILE_END_NOTIFY cmd_id=... file_id=... last=...
P4: file saved path=/spiffs/rec/file-....-W.mp3 bytes=...
C6: FILE_END_RESULT cmd_id=... file_id=... result=0x00
Server: {"type":"FILE_END","verify_ok":true}
```

### 7.2 LIVE 정상 로그 흐름

```text
C6: [LIVE] start session=... timeout=30s
P4: LIVE_CTRL_START session=... codec=... frame_ms=40 sr=16000 timeout=30s record=0
P4: READY sent status=0 reason=0x00 session=...
C6: [LIVE] ready status=0 reason=0x00 session=...
C6: [LIVE] P4 ready
C6: [AUDIO] start ...
P4: [LIVE_DIAG] rx session=... seq=1 frame_ms=40 opus_len=...
P4: jitter buf=... pkt_q=... opus_q=... pcm_q=...
```

### 7.3 LIVE 오류 로그 흐름

```text
C6: [AUDIO] stalled >3000ms, stop live
C6: LIVE_CTRL_STOP reason=0x0003
P4: LIVE_CTRL_STOP session=... reason=0x0003
P4: stop done ...
```

## 8. 빠른 판단 기준

- 서버가 `FILE_START`를 보냈는데 P4 로그에 `[CMD] FILE_START`가 없으면 C6 JSON 파싱 또는 SDIO 전달 전 단계 문제다.
- P4 로그에 `[CMD] FILE_START`는 있는데 `FILE_END_NOTIFY`가 없으면 C6 HTTPS 다운로드 또는 FILE_CHUNK 전달 문제다.
- P4가 `FILE_END result`를 보냈는데 서버가 결과를 못 받으면 C6의 CMD WSS 송신 문제다.
- 서버가 `LIVE_START`를 보냈는데 C6가 `/audio`에 접속하지 않으면 P4 READY 실패 또는 C6 ready wait 상태를 먼저 봐야 한다.
- C6는 `/audio`에 접속했는데 P4에 `LIVE_DIAG`가 없으면 AUDIO WSS 수신 또는 SDIO LIVE_FRAME 전달 문제다.
- P4에 `LIVE_DIAG`는 있는데 소리가 끊기면 P4 내부 jitter, Opus decode, PCM queue, I2S DMA 쪽을 본다.
