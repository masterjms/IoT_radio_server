# 01. 시스템 개요

## 1. 개발 목표

본 개발의 목표는 `ESP32-P4 + ESP32-C6` 조합으로 아래 두 종류의 방송 기기를 구성하는 것이다.

- 서버에 접속하여 `실시간 음성 방송(Opus)`과 `파일(HTTPS)`을 받아 재생하는 기기
- RF 모듈을 통해 수신한 방송을 재생하는 기기

현재 문서 범위는 첫 번째 항목, 즉 `서버 연동형 방송 기기`를 중심으로 정리한다.

## 2. 시스템 구조

### P4 (`D:\p4\examples\ESP-IDF\iot_radio`)

- LVGL UI
- 키 입력, 상태 표시, 로컬 제어
- Opus decode 및 오디오 출력
- 파일 수신 버퍼 관리, SHA256 검증, 저장, 재생
- RTC/센서/RF 연동
- C6와의 `SDIO RPC` 호스트

### C6 (`D:\p4\slave`)

- Wi-Fi STA 연결 및 유지
- SNTP 시간 동기화
- TLS 인증서 검증
- `WSS /cmd`, `WSS /audio` 처리
- `HTTPS` 파일 다운로드
- 서버 이벤트를 `SDIO RPC`로 P4에 전달

### 서버 (`D:\p4\examples\ESP-IDF\iot_radio\test_server`)

- `iot_radio_test_server.py`
- WSS `/cmd` 명령 서버
- WSS `/audio` Opus 바이너리 스트림 서버
- HTTPS 단일 파일 서버
- 시험용 `cert.pem`, `key.pem`, `audio_data` 제공

## 2-1. 용어 정리

문서를 다시 볼 때 가장 먼저 헷갈리는 용어를 여기서 고정한다.

- `LIVE`: 실시간 음성 방송. Opus 프레임이 WSS 바이너리로 들어온다.
- `FILE`: 다운로드 후 재생하는 파일 방송. HTTPS로 내려받는다.
- `CMD WSS`: 제어 명령용 WebSocket. `LIVE_START`, `LIVE_STOP`, `FILE_START`가 이쪽으로 들어온다.
- `AUDIO WSS`: 라이브 오디오용 WebSocket. 바이너리 Opus payload만 받는다.
- `SDIO RPC`: P4와 C6 사이의 내부 전송 채널이다. 서버 프로토콜이 아니다.
- `record_flash`: 라이브 수신 중 Opus를 저장해 둘지 여부다.
- `store_flash`: 파일 다운로드 성공 후 flash 파일로 저장할지 여부다.
- `autoplay`: 파일 다운로드 성공 후 바로 재생할지 여부다.
- `TIME_SET`: P4가 C6에 RTC 시간을 알려주는 메시지다.
- `TIME_SYNC_RESULT`: C6가 SNTP 결과를 P4에 알려주는 메시지다.

## 2-2. 한눈에 보는 구조

```text
Server
  |-- WSS /cmd ------------> C6 ---- SDIO RPC ----> P4 UI / Audio / Storage
  |-- WSS /audio ----------> C6 ---- SDIO LIVE ---> P4 Opus decode / playback
  '-- HTTPS file ----------> C6 ---- SDIO FILE ---> P4 SHA256 / autoplay / save
```

핵심은 `서버 <-> C6`, `C6 <-> P4` 두 구간으로 나뉜다는 점이다.
P4는 서버 프로토콜을 직접 처리하지 않는다.

## 3. 기본 연결 방식

서버 연결 방식은 `단일 도메인 + 경로 분리`로 확정한다.

단말에는 host만 저장한다. 예:

```text
iotradio.co.kr
```

현재 시험 포트/경로는 아래와 같다.

- WSS CMD: `wss://<host>:9001/cmd`
- WSS AUDIO: `wss://<host>:9001/audio`
- HTTPS FILE: `https://<host>:9002/<filename>` 또는 `https://<host>:9002/file`

서브도메인 방식인 `cmd.<host>`, `audio.<host>`, `file.<host>`는 현재 사양에서 사용하지 않는다.

`9001`, `9002`는 개발/시험용 포트다. 운영에서는 현장 방화벽과 서버 구성에 맞춰 `443` 같은 포트로 변경할 수 있다. 단, 단말 설정, C6 설정, 서버 listen port, 방화벽/NAT, `FILE_START.https_url`은 같은 포트 정책을 따라야 한다.

## 4. 실제 데이터 경로

### 라이브 방송

1. 서버가 `LIVE_START`를 CMD WSS로 C6에 전달
2. C6가 P4에 `LIVE_CTRL_START` 전송
3. P4가 오디오 경로 준비 후 `LIVE_CTRL_READY` 반환
4. C6가 AUDIO WSS에서 Opus 바이너리 프레임 수신
5. C6가 `SDIO_RPC_LIVE_FRAME(0x0401)`으로 P4에 전달
6. P4가 jitter buffer, Opus decode, I2S 출력 수행

### 파일 다운로드

1. 서버가 `FILE_START`를 CMD WSS로 C6에 전달
2. C6가 `https_url`, `size`, `sha256`, `store_flash`, `autoplay`, `file_name`을 파싱
3. C6가 P4에 `FILE_META(0x0500)` 전송
4. C6가 HTTPS로 파일을 읽어 `FILE_CHUNK(0x0501)`로 분할 전달
5. P4가 PSRAM에 적재하고 끝에서 SHA256 검증
6. 검증 성공 시 재생 또는 저장 수행 후 결과를 C6에 반환

## 4-1. 누가 무엇을 결정하는가

| 결정 항목 | 권한자 | 이유 |
|---|---|---|
| Wi-Fi 연결/재접속 | C6 | 실제 네트워크 스택 소유 |
| TLS 시작 가능 여부 | C6 | SNTP/TLS 상태를 알고 있음 |
| 라이브 세션 열기/닫기 | C6 | WSS 세션 소유 |
| 오디오 HW 준비 완료 | P4 | 실제 codec/I2S를 제어함 |
| 라이브 재생/저장 | P4 | Opus decode, playback, local save 주체 |
| 파일 무결성 최종 판정 | P4 | 최종 소비자이자 저장 주체 |
| 서버 주소 설정 | P4 | UI/설정 소유자 |

## 5. 시간 동기화 구조

- P4는 RTC epoch를 읽어 `TIME_SET(0x0124)`로 C6에 전달한다.
- C6는 RTC 유효 상태와 last sync age를 받아 TLS 시작 가능 여부를 판단한다.
- C6는 `time.kriss.re.kr`, `ntp.kornet.net`, `time.bora.net` 순으로 SNTP 동기화를 시도한다.
- SNTP 성공 시 `TIME_SYNC_RESULT(0x0123)`를 P4로 보내고, P4는 RTC를 갱신한다.
- `rtc_valid=0`이고 `time_ok=0`이면 C6는 WSS/HTTPS 시작을 막는다.

## 6. 현재 구조의 의미

이 구조의 목적은 단순한 기능 분리가 아니라 `P4 메모리 보호`다.
TLS/WSS/HTTPS를 P4에 다시 넣으면 UI, 오디오, 저장과 경쟁하게 되므로 현재 구조는 유지해야 한다.

## 7. 다시 볼 때 기억할 핵심 문장

- `C6는 서버 담당, P4는 사용자 기능 담당`이다.
- `라이브는 WSS`, `파일은 HTTPS`다.
- `P4는 SDIO로만 서버 데이터를 본다`.
