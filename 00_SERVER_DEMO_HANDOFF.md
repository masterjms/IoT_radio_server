# 서버 데모 개발 전달 문서

작성일: 2026-06-16

## 1. 목적

이 문서는 Windows 11 환경에서 iot radio 서버 데모 버전을 만들기 위해 서버 개발자 또는 Codex에게 전달할 핵심 내용을 정리한 문서다.

서버는 ESP32-P4와 직접 통신하지 않는다. 서버의 직접 통신 대상은 ESP32-C6이며, C6가 받은 명령과 데이터를 SDIO RPC로 ESP32-P4에 전달한다.

```text
Server
  |-- WSS /cmd -----> ESP32-C6 ---- SDIO RPC ----> ESP32-P4
  |-- WSS /audio ---> ESP32-C6 ---- SDIO RPC ----> ESP32-P4
  '-- HTTPS file ---> ESP32-C6 ---- SDIO RPC ----> ESP32-P4
```

## 2. 서버 개발자가 우선 읽을 파일

아래 파일만 먼저 전달하면 서버 데모 구현에 필요한 내용은 대부분 충족된다.

1. `spec/esp32_p4_c6_spec_v2.0.0/01_SYSTEM_OVERVIEW.md`
   - 전체 구조와 P4/C6/서버 역할 구분
2. `spec/esp32_p4_c6_spec_v2.0.0/05_CURRENT_STATUS.md`
   - 현재 단말 코드가 실제로 지원하는 범위
3. `spec/esp32_p4_c6_spec_v2.0.0/06_SERVER_INTERFACE.md`
   - 서버 JSON 명령, WSS/HTTPS 경로, 에러 코드
4. `spec/esp32_p4_c6_spec_v2.0.0/07_LIVE_BURST_POLICY.md`
   - 실시간 Opus 송신 간격, catch-up, drop 정책
5. `spec/esp32_p4_c6_spec_v2.0.0/08_FLOW_EXAMPLES.md`
   - FILE/LIVE 정상 흐름과 오류 흐름 예제

`02_ROLE_SPLIT.md`, `03_RUNTIME_FLOW.md`, `04_MEMORY_POLICY.md`는 단말 내부 이해용이다. 서버 데모 개발에는 보조 문서로만 사용한다.

## 3. 데모 서버 기본 환경

- OS: Windows 11
- 언어: Python 권장
- 실행 형태: 단일 콘솔 프로그램 우선
- 서버 기능:
  - WSS 명령 채널
  - WSS 오디오 채널
  - HTTPS 파일 다운로드
  - 콘솔 또는 간단한 UI에서 LIVE/FILE 명령 전송

현재 프로젝트에 있는 시험 서버 위치:

```text
D:\xWIFI_Radio\iot_radio\test_server
```

참고 파일:

```text
D:\xWIFI_Radio\iot_radio\test_server\iot_radio_test_server.py
D:\xWIFI_Radio\iot_radio\test_server\README.md
```

## 4. 운영 도메인 기준 연결 방식

내부 IP 예시인 `192.168.0.5`는 과거 로컬 시험값이다. 서버 데모 문서와 신규 서버 구현은 도메인 기준으로 설명한다.

예시 도메인:

```text
iotradio.co.kr
```

서버 연결 방식은 `단일 도메인 + 경로 분리`로 확정한다.

단말에는 host만 저장한다.

```text
iotradio.co.kr
```

단말과 C6는 이 host를 기준으로 아래 URI를 만든다.

```text
CMD WSS   : wss://iotradio.co.kr:9001/cmd
AUDIO WSS : wss://iotradio.co.kr:9001/audio
HTTPS FILE: https://iotradio.co.kr:9002/<filename>
```

서브도메인 방식은 현재 사양에서 사용하지 않는다.

```text
사용하지 않음:
wss://cmd.iotradio.co.kr:9001/
wss://audio.iotradio.co.kr:9001/
https://file.iotradio.co.kr/<filename>
```

이렇게 확정하는 이유는 DNS, TLS 인증서, 방화벽, 서버 라우팅 조건을 단순하게 유지하기 위해서다. 인증서도 `iotradio.co.kr` 기준으로 맞추면 된다.

### 포트 정책

`9001`, `9002`는 현재 개발/시험용 포트다.

```text
시험 기본값:
WSS   9001
HTTPS 9002
```

운영에서는 현장 방화벽과 서버 구성에 맞춰 문제가 없는 포트로 변경할 수 있다. 관공서/외부망 환경에서는 일반적으로 `443` 사용이 가장 안전하다.

운영 포트 예:

```text
CMD WSS   : wss://iotradio.co.kr/cmd
AUDIO WSS : wss://iotradio.co.kr/audio
HTTPS FILE: https://iotradio.co.kr/<filename>
```

포트를 변경할 때는 단말 설정, C6 설정, 서버 listen port, 방화벽/NAT, `FILE_START.https_url`이 모두 같은 기준을 보아야 한다.

## 5. TLS 인증서 주의사항

C6는 서버 인증서를 검증한다. 데모 서버의 인증서와 C6에 포함된 신뢰 인증서가 맞지 않으면 WSS/HTTPS 연결이 실패한다.

현재 C6 인증서 파일:

```text
D:\xWIFI_Radio\slave\main\wss_server_cert.pem
```

서버 데모에서 자체 서명 인증서를 쓰는 경우:

- 서버의 `cert.pem`과 C6 내장 `wss_server_cert.pem`의 CA/인증서 관계가 맞아야 한다.
- 도메인으로 접속할 경우 인증서 CN 또는 SAN이 접속 host와 맞아야 한다.
- IP로 접속할 경우 인증서가 IP SAN을 포함하지 않으면 TLS 검증에서 실패할 수 있다.

## 6. CMD WSS JSON 명령 요약

### LIVE_START

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

서버는 `LIVE_START` 이후 C6가 `/audio`에 접속하면 Opus binary frame을 전송한다. WSS binary message 1개는 Opus frame 1개다.

`sample_rate`의 기본/권장값은 `16000`이다. C6 코드도 `sample_rate`가 없거나 `0`이면 `16000`으로 보정한다. 단, 서버가 실제로 전송하는 Opus 파일이 48 kHz로 만들어진 경우에는 해당 음원에 맞춰 `48000`을 보낼 수 있다.

### LIVE_STOP

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

### FILE_START

```json
{
  "type": "FILE_START",
  "ver": 267,
  "cmd_id": 100,
  "file_id": 7,
  "https_url": "https://iotradio.co.kr:9002/file-1779067243.mp3",
  "size": 73990,
  "sha256": "<64 hex>",
  "store_flash": true,
  "autoplay": true,
  "file_name": "file-1779067243.mp3",
  "resume_offset": 0
}
```

현재 단말 구현은 `resume_offset=0`만 지원한다.

## 7. 저장 파일명 정책

서버가 `file_name`을 전달하면 P4는 이 이름을 기준으로 `/spiffs/rec`에 저장한다.

- FILE 예: `notice-<epoch>.mp3` 또는 `file-<epoch>.mp3`
- LIVE 예: `live-<epoch>.lopus`
- 저장 시 수동 재생 전 상태 표시로 `-W`를 붙인다.

최종 저장 예:

```text
/spiffs/rec/notice-1779067243-W.mp3
/spiffs/rec/live-1779067243-W.lopus
```

서버가 `file_name`에 epoch를 넣지 않으면 P4가 저장 시점 RTC epoch를 붙인다.

## 8. 실시간 Opus 송신 핵심

- 기본 frame 간격: 40 ms
- WSS binary message 1개 = Opus frame 1개
- 여러 Opus frame을 하나의 binary message로 합치지 않는다.
- 지연이 누적되면 오래된 frame은 버리고 현재 시점에 가까운 frame을 우선한다.
- 자세한 pacing 정책은 `07_LIVE_BURST_POLICY.md`를 따른다.

## 9. AUDIO WSS Opus payload 규격

서버가 `/audio` WSS로 보내는 기본 형식은 header 없는 순수 Opus packet이다.

```text
WSS binary payload = opus_packet_bytes
```

즉, 서버는 아래를 보내면 안 된다.

- `.opus` 파일 전체를 그대로 전송
- Ogg page 전체
- `OggS`로 시작하는 Ogg container bytes
- `OpusHead`
- `OpusTags`
- 여러 Opus packet을 하나의 WSS binary message로 합친 데이터

현재 P4 코드는 수신 초기에 payload가 `OggS`, `OpusHead`, `OpusTags`처럼 보이면 경고 로그를 남기고 drop한다. 따라서 서버 데모는 Ogg Opus 파일을 사용할 경우 Ogg page를 풀어서 실제 audio Opus packet만 추출한 뒤, packet 1개를 WSS binary message 1개로 보내야 한다.

현재 `test_server/iot_radio_test_server.py`는 이 방식으로 동작한다.

- Ogg Opus 파일이면 `OpusHead`, `OpusTags`를 건너뛰고 audio packet만 추출
- raw packet 파일이면 `2바이트 big-endian 길이 + Opus payload` 반복 형식으로 읽음
- 전송 시에는 길이 prefix 없이 Opus payload만 `ws.send(pkt)`로 전송

### 선택형 LFRM wrapper

C6 코드에는 선택적으로 `LFRM` wrapper를 해석하는 코드도 들어 있다. 다만 1차 서버 데모에서는 사용하지 않는 것을 권장한다. 기본은 순수 Opus packet 전송이다.

`LFRM` wrapper 형식:

```text
offset  size  의미
0       4     ASCII "LFRM"
4       8     reserved, 현재 C6 코드에서는 사용하지 않음
12      2     frame_ms, big-endian
14      2     opus_payload_len, big-endian
16      N     opus_payload
```

`LFRM`을 사용하면 C6는 wrapper를 제거하고 `opus_payload`만 P4로 전달한다.

### C6 -> P4 내부 LIVE_FRAME

서버가 보내는 WSS payload와 C6가 P4에 보내는 SDIO payload는 다르다. 서버는 SDIO header를 붙이지 않는다. C6가 내부적으로 아래 12바이트 LIVE_FRAME header를 붙인다.

```text
offset  size  의미
0       4     session_id, big-endian
4       4     seq, big-endian
8       2     frame_ms, big-endian
10      2     opus_payload_len, big-endian
12      N     opus_payload
```

P4는 이 내부 LIVE_FRAME에서 `opus_payload`만 꺼내 Opus decoder에 넣는다.

## 10. 서버가 받아야 하는 결과

FILE 완료 결과 예:

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
  "fail_reason": "STORAGE_FAIL"
}
```

P4 LIVE 상태 통계는 `LIVE_STATS`로 올라올 수 있다. 데모 서버는 우선 로그로 출력만 해도 된다.

## 11. 현재 코드 기준 주의사항

- 서버 직접 상대는 C6다.
- P4는 서버 JSON을 직접 파싱하지 않는다.
- FILE과 LIVE는 동시에 처리하지 않는다.
- FILE 진행 중 LIVE가 들어오면 LIVE가 우선이고 FILE은 중단된다.
- 자동 재생 파일은 현재 MP3 중심이다.
- LIVE 저장 파일은 Opus stream 형태의 `.lopus`를 사용한다.
- `/spiffs`는 실제 mount path 이름이며, 문서상 LittleFS 저장소를 의미한다.

## 12. 서버 데모 최소 기능 목록

1. WSS `/cmd` 서버
2. WSS `/audio` 서버
3. HTTPS 파일 서버
4. LIVE_START 전송
5. Opus frame pacing 전송
6. LIVE_STOP 전송
7. FILE_START 전송
8. FILE_END / FILE_ABORT / LIVE_STATS 로그 출력
9. 인증서 파일 관리
10. 실행 README 작성

## 13. 과거 시험값 정리

아래 값은 과거 내부 시험값이며 신규 서버 데모 문서에서는 기준값으로 사용하지 않는다.

- `192.168.0.5`
- `test_test.com`
- `D:\p4\examples\ESP-IDF\iot_radio`
- `D:\p4\slave`

신규 기준 경로:

```text
P4 프로젝트 : D:\xWIFI_Radio\iot_radio
C6 프로젝트 : D:\xWIFI_Radio\slave
시험 서버   : D:\xWIFI_Radio\iot_radio\test_server
```

신규 기준 예시 도메인:

```text
iotradio.co.kr
```
