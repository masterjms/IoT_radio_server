# 05. 현재 상태

## 1. 확인된 구현 상태

### P4

- LVGL 기본 UI 동작
- 키 입력 및 `app_control` 연동
- `live_ctrl_task` 기반 라이브 준비/정지 제어
- `live_audio_task` 기반 Opus 수신 큐, jitter, decode, 재생
- `file_rx` 기반 파일 메타/청크 수신
- SHA256 검증 후 자동 재생/플래시 저장
- RTC, HDC1080, RF 모듈 연동

### C6

- Wi-Fi STA 연결 확인
- SNTP 동기화 확인
- CMD WSS 연결 루프, 재접속 백오프 구현
- AUDIO WSS 연결 및 바이너리 프레임 전달 구현
- HTTPS 단일 파일 다운로드 구현
- WSS/HTTPS 오류 시 FILE/LIVE 정리 로직 구현

### 테스트 서버

- 현재 권장 서버 폴더: `D:\p4\examples\ESP-IDF\iot_radio\test_server`
- 현재 권장 통합 서버: `iot_radio_test_server.py`
  - WSS `/cmd`
  - WSS `/audio`
  - HTTPS 단일 파일 서버
  - LIVE/FILE 송신
  - `record_flash` / `store_flash` 선택 시험

## 2. 현재 코드 기준 확정 사항

- 라이브 시작 시 `record_flash`를 C6가 받아 P4에 전달한다.
- 라이브 준비 timeout 값은 `1~60초`, 기본 `30초`로 C6/P4 양쪽에서 보정된다.
- 현재 P4 준비 루틴은 동기식으로 빠르게 READY를 보내므로, 이 timeout은 평상시 자주 발동하는 기능이 아니라 PREPARING 상태 보호용 안전장치에 가깝다.
- C6는 `audio_url`, `https_url` override를 JSON에서 직접 받을 수 있다.
- 파일 다운로드는 `sha256`을 함께 받아 P4가 최종 검증한다.
- 파일 다운로드가 진행 중일 때 `LIVE_START`가 오면 파일은 즉시 중단된다.

## 3. 아직 구현이 단순하거나 제한적인 부분

- P4 파일 재생은 현재 `MP3` 기준으로 구현되어 있다.
- `resume_offset`은 아직 실제 지원하지 않는다.
- 파일 credit/HOLD 정책은 원본 사양만큼 세분화되어 있지 않다.
- 사양 문서의 일반화된 2채널/1채널 mux 옵션 중 현재 시험 기본은 `CMD / AUDIO` 2경로다.

## 3-1. 원본 사양과 현재 코드의 차이

나중에 헷갈리지 않도록 현재 기준 차이를 명확히 적는다.

- 원본 사양: 파일 resume를 정의
- 현재 코드: `resume_offset != 0`이면 중단

- 원본 사양: 파일 타입을 비교적 일반화
- 현재 코드: 자동 재생 경로는 MP3 검사/재생 흐름이 중심

- 원본 사양: credit/HOLD 제어를 더 풍부하게 설명
- 현재 코드: 파일 쪽은 단순 다운로드 브리지 중심

- 원본 사양/정책: LIVE 송신 지연 회복용 bounded burst와 오래된 frame drop 정책을 정의
- 현재 테스트 서버 코드: 기본 cadence 송신이며, 200ms 이내 지연은 sleep 없이 자연 catch-up할 수 있지만 `1600ms/2000ms` 기준의 명시적 drop 정책은 아직 구현되어 있지 않음

- 현재 코드 기준: 테스트 서버는 `D:\p4\examples\ESP-IDF\iot_radio\test_server\iot_radio_test_server.py`

## 4. 실제 시험 시 주의점

- C6는 `D:\p4\slave\main\wss_server_cert.pem`을 신뢰한다.
- 임시 서버에서 다른 인증서를 쓰면 TLS가 실패한다.
- 개발/시험용 기본 포트는 WSS `9001`, HTTPS `9002`다.
- 운영에서는 방화벽과 서버 구성에 맞춰 포트를 변경할 수 있으며, 관공서/외부망 환경에서는 `443` 사용을 우선 검토한다.
- 기본 라이브 파일은 `audio_data\voice001_mono_40ms.opus`다.
- 기본 파일 다운로드 대상은 `audio_data\voice001.mp3`다.

## 5. 권장 시험 순서

1. Wi-Fi 연결과 SNTP 동기화 확인
2. CMD WSS 연결 확인
3. `LIVE_START`로 실시간 방송 확인
4. `FILE_START`로 MP3 다운로드/재생 확인
5. LIVE 중 FILE 선점, FILE 중 LIVE 선점 같은 예외 흐름 확인

## 5-1. 지금 바로 확인해야 하는 로그 포인트

- P4:
  - `LIVE_CTRL_START`
  - `LIVE_CTRL_STOP`
  - `[CMD] FILE_START`
  - `[CMD] FILE_END_NOTIFY`
  - `file saved path=...`

- C6:
  - `[SNTP] synced`
  - `[WSS] connected`
  - `[LIVE] ready status=0`
  - `[FILE] end result ... result=0x00`

## 6. 유지 결론

- 현재 구조는 `P4 메모리 보호`라는 목표에 맞다.
- 다음 단계도 `C6 네트워크 전담`, `P4 재생/저장 전담` 구조를 유지해야 한다.
- 향후 확장은 문서상 기능보다 `현재 코드가 실제로 허용하는 값과 포맷`을 기준으로 진행하는 것이 맞다.

## 7. 한 줄 결론

현재 시스템은 `C6가 서버와 통신하고, P4가 결과를 소비하는 구조`로 이해하면 거의 맞다.
