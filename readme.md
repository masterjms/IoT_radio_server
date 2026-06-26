# IoT Radio — 실시간 방송 중계 시스템

군포(방송국)에서 송출한 음성·파일 방송을 서울 AWS Lightsail 중계 서버를 거쳐 군산에 배치된 ESP32 단말로 전달하는 시스템이다. 이 문서는 전체 아키텍처, 서버·앱 파일 구조, AWS 설정 절차, 데이터 흐름, 통신 방식, 배포, 모니터링 방침을 정리한 개발 기준 문서다.

---

## 1. 시스템 개요

방송은 군포의 송출 PC에서 시작해 서울 중계 서버를 거쳐 군산 단말에서 재생된다. 군포와 군산은 서로 직접 연결되지 않고 항상 서울 서버를 경유한다.

```
[군포 방송국]              [서울 Lightsail 중계]            [군산 단말]
네이티브 앱        →       nginx (443, TLS 종단)      →     ESP32-C6
 - 마이크 송출            Python 서버                       (WSS·HTTPS)
 - 파일 업로드            (페이싱·팬아웃·파일관리)              ↓ SDIO RPC
 - 상태 모니터링          정적 파일 서빙(nginx)              ESP32-P4
                                                          (Opus 디코드·재생·저장)
                                                              ↓
                                                            스피커
```

단말은 두 칩으로 구성된다. C6가 서버와의 모든 네트워크 통신(WSS/HTTPS)을 담당하고, 받은 데이터를 SDIO RPC로 P4에 넘긴다. P4는 Opus 디코드, 재생, 저장, SHA256 검증을 담당한다. 서버는 P4와 직접 통신하지 않으며, 항상 C6를 상대한다.

### 두 개의 설계 영역

이 시스템은 성격이 다른 두 구간으로 나뉜다. 이 구분이 전체 설계의 핵심이다.

**서버↔단말 구간**은 단말 사양 문서(`00`~`08`)가 규정한 영역이다. 서버가 단말과 어떻게 통신해야 하는지를 단말 구현 입장에서 정의한 계약서이며, 우리는 이 계약을 그대로 지켜야 한다.

**방송국↔서버 구간**은 사양 문서에 없는 신규 설계 영역이다. 군포 앱이 서버로 오디오와 파일을 어떻게 보내는지는 우리가 정의한다. 사양 문서의 서버는 본래 로컬 음원을 직접 송출하는 데모 서버였고, 외부에서 음원을 받아 중계하는 구조는 우리가 추가하는 부분이다.

---

## 2. 서버 스펙

| 항목 | 값 |
| --- | --- |
| 서비스 | AWS Lightsail |
| 리전 | 서울 (ap-northeast-2) |
| 메모리 | 1 GB |
| vCPU | 2 |
| 스토리지 | 40 GB SSD |
| 전송량 | 2 TB / 월 |
| OS | Ubuntu LTS |

서울 리전은 군산 단말과의 왕복 지연(RTT)을 최소화하기 위한 필수 선택이다. 1GB RAM은 Python 서버 운용에 여유가 있으며, swap 2GB를 안전장치로 둔다.

---

## 3. 개발 도구

서버는 Python으로 작성하고 asyncio 기반 WebSocket 서버로 구성한다. 앞단에는 nginx를 두어 TLS 종단, 경로 라우팅, 정적 파일 서빙을 맡긴다. 오디오 변환과 마이크 캡처에는 FFmpeg을 사용한다. TLS 인증서는 Let's Encrypt(certbot)로 발급·자동 갱신한다.

| 영역 | 도구 |
| --- | --- |
| 서버 언어 | Python (asyncio, websockets) |
| 리버스 프록시 | nginx |
| TLS 인증서 | Let's Encrypt / certbot |
| 오디오 | FFmpeg, Opus 코덱 |
| 서비스 관리 | systemd |
| 로그 | journald |
| 배포 | Git, GitHub Actions |
| 단말 통신 | WSS, HTTPS, SDIO RPC |

---

## 4. AWS Lightsail 설정

AWS를 처음 다루는 경우를 기준으로, 인스턴스 생성부터 인증서 발급까지의 순서를 정리한다.

### 4.1 인스턴스 생성

Lightsail 콘솔(lightsail.aws.amazon.com)에서 "인스턴스 생성"을 누른다. 리전은 반드시 서울(ap-northeast-2)을 고르고, 플랫폼은 Linux/Unix, 블루프린트는 "OS 전용"의 Ubuntu LTS를 선택한다. 플랜은 1GB RAM / 2 vCPU / 40GB SSD / 2TB를 고른 뒤 인스턴스 이름(예: `iotradio-server`)을 지정해 생성한다.

### 4.2 고정 IP 연결

기본 IP는 재부팅 시 바뀔 수 있으므로, "네트워킹" 탭에서 고정 IP(Static IP)를 생성해 인스턴스에 연결한다. 인스턴스에 연결된 고정 IP는 무료다.

### 4.3 도메인 DNS 연결

TLS 인증서 발급에 도메인이 필요하므로 도메인을 구매해 사용한다. 도메인 등록처의 DNS 관리에서 A 레코드를 추가해 도메인이 고정 IP를 가리키게 한다.

```
유형: A    이름: @      값: <고정 IP>
유형: A    이름: www    값: <고정 IP>
```

도메인 확정은 단말 펌웨어와 직결된다. 단말에는 host가 저장되고 C6가 그 host로 인증서를 검증하므로, 최종 도메인을 확정하고 펌웨어 담당자와 인증서 신뢰 관계를 먼저 합의해야 한다.

### 4.4 방화벽 설정

"네트워킹" 탭의 방화벽 규칙을 아래 상태로 맞춘다. 시험용 포트 9001/9002는 열지 않으며, 운영은 443 단일 포트로 통합한다.

```
허용: 22   (SSH 관리 접속)
허용: 80   (HTTP, 인증서 발급 및 HTTPS 리다이렉트)
허용: 443  (HTTPS/WSS 서비스)
```

### 4.5 SSH 접속

초기에는 Lightsail 콘솔의 "SSH를 사용하여 연결" 버튼으로 브라우저 터미널을 띄워 작업한다. 본격적인 작업은 내 PC에서 SSH 키(.pem)를 내려받아 접속한다.

```bash
ssh -i 다운로드한키.pem ubuntu@<고정 IP>
```

### 4.6 기본 소프트웨어 설치

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y nginx python3 python3-venv python3-pip ffmpeg git
sudo snap install --classic certbot
```

### 4.7 swap 설정

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
```

### 4.8 TLS 인증서 발급

DNS가 도메인을 서버로 가리키게 된 뒤 실행한다.

```bash
sudo certbot --nginx -d <도메인> -d www.<도메인>
```

certbot이 인증서를 발급하고 nginx에 HTTPS 설정을 넣으며 90일마다 자동 갱신한다.

**중요:** Let's Encrypt 인증서를 쓰려면 C6 펌웨어의 신뢰 인증서가 Let's Encrypt 루트 CA(ISRG Root X1)를 신뢰해야 한다. 현재 C6에 내장된 인증서가 무엇인지 펌웨어 담당자와 먼저 맞춰야 한다. 이게 어긋나면 서버는 정상이어도 단말이 연결을 거부한다.

---

## 5. 파일 구조

### 5.1 서버 (`/opt/iotradio/`)

```
/opt/iotradio/                  프로젝트 루트 (Git 저장소)
├── app/                        Python 서버 코드
│   ├── server.py               진입점. asyncio로 전체 서버 기동
│   ├── config.py               포트, 경로, 도메인, 인증 토큰 등 설정
│   ├── cmd_channel.py          /cmd WSS — LIVE/FILE 명령 송신, C6 결과 수신
│   ├── audio_channel.py        /audio WSS — Opus 프레임을 C6에 팬아웃
│   ├── ingest_channel.py       /ingest WSS — 군포 앱이 보내는 Opus 프레임 수신
│   ├── pacing.py               07 정책 페이싱 엔진 (backlog/burst/drop)
│   ├── opus_source.py          로컬 .opus 파일에서 audio packet 추출 (테스트용)
│   ├── file_manager.py         업로드 저장, SHA256 계산, 파일 목록 관리
│   ├── session.py              상태 머신 (IDLE / LIVE / FILE), 선점 규칙
│   ├── registry.py             device_id → WebSocket 매핑
│   ├── http_api.py             /upload, /broadcast, /api/* HTTP 엔드포인트
│   └── logging_conf.py         표준 로그 포맷
│
├── media/                      오디오 파일 저장소
│   ├── uploads/                군포 앱이 올린 MP3 (nginx가 직접 서빙)
│   └── samples/                voice001_mono_40ms.opus 등 테스트 음원
│
├── tests/                      단위 테스트 (페이싱 엔진 등 핵심 로직)
│   └── test_pacing.py
│
├── venv/                       Python 가상환경 (Git 제외)
├── requirements.txt
├── .env                        인제스트 토큰 등 비밀값 (Git 제외)
└── .gitignore                  venv/, .env, media/uploads/ 제외
```

서버는 UI를 갖지 않는다. 순수하게 채널과 API만 제공하며, 모든 화면은 군포 네이티브 앱이 들고 있다.

시스템 레벨 설정 파일은 프로젝트 밖에 위치한다.

```
/etc/nginx/sites-available/iotradio        nginx 설정
/etc/systemd/system/iotradio.service       서비스 등록
/etc/letsencrypt/                          certbot 인증서
```

### 5.2 군포 네이티브 앱 (`gunpo-broadcaster/`)

서버와는 별도 Git 저장소로 관리한다. 배포 방식과 버전 생명주기가 다르기 때문이다. 언어·프레임워크와 무관하게 아래 역할 구조를 유지한다.

```
gunpo-broadcaster/              군포 앱 (별도 Git 저장소)
├── src/
│   ├── main.*                  앱 진입점, 창 생성
│   ├── ui/                     화면
│   │   ├── live_panel.*        라이브 시작/중지, 마이크 상태
│   │   ├── file_panel.*        MP3 선택·업로드·전송 트리거
│   │   └── status_panel.*      단말 연결 상태, 현재 방송 상태 표시
│   ├── audio/
│   │   ├── mic_capture.*       FFmpeg으로 마이크 → 16kHz mono Opus 40ms 인코딩
│   │   └── ingest_sender.*     인코딩된 Opus 프레임을 /ingest WSS로 전송
│   ├── net/
│   │   ├── ingest_client.*     /ingest WebSocket 연결·인증·재접속
│   │   └── api_client.*        /upload, /broadcast, /api/* HTTP 호출
│   └── config.*                서버 도메인, 인증 토큰 등
│
├── assets/                     아이콘 등
├── build/                      빌드 산출물 (Git 제외)
└── (빌드 설정 파일)
```

`audio/mic_capture.*`가 네이티브 앱을 택한 핵심 이유다. 브라우저에 휘둘리지 않고 FFmpeg으로 처음부터 사양이 요구하는 정확한 형식(16kHz mono Opus 40ms)으로 인코딩한다. 덕분에 서버는 받은 프레임을 변환 없이 페이싱 엔진에 바로 넘길 수 있고, 서버에 별도 transcode 모듈이 필요 없다.

---

## 6. 통신 방식

### 6.1 nginx 라우팅

외부에서 들어오는 모든 요청은 443 포트의 nginx를 먼저 만난다. nginx가 TLS를 복호화하고 경로에 따라 내부로 분배한다. `/cmd`, `/audio`, `/ingest` 등은 리눅스 파일이 아니라 URL 경로이며, 실제로는 `server.py`가 띄운 하나의 서버 안에서 경로로 분기된다.

```
location /cmd       → Python WSS  (단말 명령)
location /audio     → Python WSS  (단말 오디오)
location /ingest    → Python WSS  (군포 앱 오디오 수신)
location /upload    → Python HTTP (군포 앱 파일 업로드)
location /broadcast → Python HTTP (방송 트리거)
location /api/      → Python HTTP (상태 조회)
location /files/    → nginx 정적 서빙 (media/uploads/ — FILE_START의 https_url 대상)
```

nginx의 역할은 TLS 종단에 그치지 않는다. 리버스 프록시(외부 요청을 내부 Python으로 중계), 경로 기반 라우팅, 정적 파일 직접 서빙, WebSocket 업그레이드 처리를 모두 담당한다. 특히 파일 다운로드(`/files/`)는 Python을 거치지 않고 nginx가 디스크에서 직접 응답해 서버 메모리를 아낀다.

### 6.2 서버↔단말 채널 (사양 문서 규정)

| 채널 | 용도 |
| --- | --- |
| `wss://<도메인>/cmd` | 제어 명령 채널. LIVE_START/STOP, FILE_START/STOP 송신, C6 결과 수신. 상시 유지 |
| `wss://<도메인>/audio` | 라이브 Opus 프레임 채널. LIVE_START 이후에만 연결 |
| `https://<도메인>/files/<파일명>` | 파일 다운로드. C6가 직접 당겨받음 |

핵심 규칙으로, AUDIO WSS의 binary 메시지 1개는 정확히 Opus 패킷 1개다. 여러 프레임을 하나로 합치면 안 되고, `.opus` 파일 전체나 Ogg 컨테이너, OpusHead/OpusTags를 보내서도 안 된다. Opus payload는 4000B 이하를 유지한다. LIVE는 C6가 P4 준비 완료(READY)를 확인한 뒤에야 `/audio`를 연결한다.

### 6.3 방송국↔서버 채널 (신규 설계)

| 채널 | 용도 |
| --- | --- |
| `wss://<도메인>/ingest` | 군포 앱이 인코딩한 Opus 프레임을 서버로 전송 |
| `POST /upload` | MP3 파일 업로드. 서버가 저장 후 SHA256과 URL 응답 |
| `POST /broadcast` | 업로드된 파일의 단말 전송 트리거 |
| `GET /api/*` | 서버·단말 상태 조회 |

`/ingest`의 인증 방식(토큰 등)은 신규 설계 항목으로 별도 확정이 필요하다.

---

## 7. 데이터 흐름

### 7.1 라이브 방송

```
[군포 앱]                      [서버]                          [단말]
mic_capture                                                    
 → Opus 40ms 인코딩                                            
 → /ingest WSS  ────────→  ingest_channel 수신                 
                           → pacing (07 정책)                  
                           → audio_channel 팬아웃 ──/audio──→  C6 → P4 재생
                           cmd_channel ──LIVE_START /cmd────→  C6 (준비)
```

군포 앱이 마이크를 잡아 Opus 40ms로 인코딩해 `/ingest`로 보낸다. 서버는 이를 페이싱 엔진에 넣고, CMD 채널로 단말에 LIVE_START를 보내 준비시킨 뒤, C6가 `/audio`에 접속하면 페이싱된 프레임을 팬아웃한다. 종료 시 군포 앱이 `/ingest`를 끊으면 서버가 LIVE_STOP을 보낸다.

### 7.2 파일 방송

```
[군포 앱]                      [서버]                          [단말]
file_panel                                                     
 → POST /upload  ───────→  file_manager (저장 + SHA256)        
                ←──────  {url, size, sha256} 응답              
 → POST /broadcast ─────→  cmd_channel ──FILE_START /cmd──→  C6
                           nginx /files/ ◀──HTTPS GET────────  C6 (다운로드)
                           cmd_channel ◀──FILE_END /cmd──────  C6 (결과 보고)
```

군포 앱이 MP3를 업로드하면 서버가 저장하고 SHA256을 계산해 URL과 함께 응답한다. 앱이 전송을 트리거하면 서버가 FILE_START를 단말에 보내고, C6가 그 URL로 직접 파일을 당겨받는다. 다운로드 응답은 nginx가 정적으로 처리해 Python을 거치지 않는다. C6는 완료 후 FILE_END로 결과를 보고한다.

---

## 8. 페이싱 정책 (07 문서)

서버가 40ms마다 프레임을 보낼 때, 네트워크나 OS 스케줄링으로 송신이 밀리는 경우를 위한 정책이다. 예정 송신 시각은 `live_start_time + frame_index × frame_ms`로 계산하고, 현재 시각이 이보다 늦으면 backlog가 쌓인 것으로 본다.

| backlog | 처리 |
| --- | --- |
| 0~1 frame | 정상 pacing |
| 2~3 frame | 제한형 catch-up 허용 |
| 4 frame 이상 | catch-up 허용, 단 1회 burst는 120ms(40ms 기준 3 frame) 이하 |
| 1600ms 초과 | 오래된 frame 폐기 시작 |
| 2000ms 초과 | 반드시 폐기 |

무제한 burst는 금지한다. 실시간 방송에서는 늦은 음성을 모두 살리는 것보다 현재 시점에 맞추는 것이 우선이다. LIVE_STOP 이후 남은 backlog 프레임도 즉시 폐기한다.

P4는 `LIVE_STATS`로 `p4_buffer_ms`, `underrun_count`를 보고할 수 있고, 서버는 이 값을 송신 속도 조정에 참고할 수 있다. 다만 이는 관측값이므로 서버는 자체 backlog도 함께 계산해야 한다.

---

## 9. 모니터링
서버 코드가 스스로 상태를 드러내는 가벼운 모니터링을 둔다. 감시 지점은 세 곳이다.

**오디오 파이프라인 헬스**가 가장 중요하다. `pacing.py`와 `audio_channel.py`가 drop·burst·backlog 수치와 P4의 `p4_buffer_ms`, `underrun_count`를 주기적으로 로그에 남긴다. 방송 품질 문제를 진단하는 핵심 지표다.

**시스템 자원**은 1GB RAM의 메모리·CPU·swap 추세를 본다. systemd의 `MemoryMax` 제한과 `systemctl status`, Lightsail 콘솔의 무료 그래프로 충분하다. 메모리 누수는 장시간 soak 테스트로 추세를 관찰해 잡는다.

**연결 상태**는 단말(C6)과 군포 앱의 접속 여부, WSS heartbeat(ping/pong) 응답을 추적한다. `registry.py`와 각 채널이 연결·해제 이벤트를 로그로 남긴다.

이 모든 로그는 journald가 자동 수집하며 `journalctl -u iotradio -f`로 실시간 확인한다. 운영이 안정화되면 서버가 `/api/health`로 연결 수·drop 카운터·메모리를 JSON으로 노출하고, 군포 앱의 status_panel이 이를 주기적으로 읽어 화면에 표시하는 방식으로 확장한다.

---

## 10. 배포 (CD)

GitHub Actions로 배포를 자동화한다. Python 서버라 빌드 단계가 없으므로 CI(테스트·빌드 검증)는 처음부터 무겁게 가지 않고, 페이싱 엔진 같은 핵심 로직이 자리잡으면 단위 테스트를 점진적으로 붙인다.

배포 흐름은 GitHub에 SSH 키를 Secret으로 등록해두고, 워크플로가 그 키로 서버에 접속해 정해진 명령을 실행하는 구조다.

```
워크플로 실행
  → SSH로 Lightsail 접속
  → cd /opt/iotradio && git pull
  → systemctl restart iotradio
```

라이브 방송 중 배포가 일어나면 서비스 재시작으로 방송이 끊긴다. 따라서 배포 트리거는 자동 push가 아니라 수동 실행(`workflow_dispatch`)을 기본으로 둔다. nginx 설정을 바꿨을 때만 추가로 `systemctl reload nginx`를 실행한다.

### systemd 서비스

서버가 재부팅돼도 자동으로 켜지고 죽으면 다시 살아나도록 등록한다. `/etc/systemd/system/iotradio.service`:

```ini
[Unit]
Description=IoT Radio Server
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/iotradio
ExecStart=/opt/iotradio/venv/bin/python app/server.py
Restart=on-failure
MemoryMax=700M

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable iotradio
sudo systemctl start iotradio
journalctl -u iotradio -f
```

---

## 11. 개발 순서

의존 관계를 기준으로 한 진행 순서다. 인프라가 토대이고, 라이브 파이프라인이 가장 공을 들여야 할 핵심이다.

**1단계 — 인프라.** Lightsail 인스턴스 생성, 고정 IP, 도메인 DNS 연결, nginx, swap, 방화벽, TLS 인증서. 이 단계가 끝나면 도메인으로 HTTPS 접근이 가능하다.

**2단계 — 서버↔단말 파일 방송.** 가장 단순한 흐름으로 단말 파이프라인을 먼저 검증한다. CMD WSS와 nginx 정적 서빙으로 FILE_START → 다운로드 → FILE_END 왕복을 확인한다. WSS 연결, 인증서 신뢰, JSON 파싱, SDIO 전달이 한 번에 검증된다.

**3단계 — 서버↔단말 라이브 (서버 로컬 파일).** 방송국 연결 없이 서버의 로컬 Opus 파일을 직접 송출해 라이브 파이프라인을 완성한다. AUDIO WSS와 07 페이싱 엔진을 구현하는 가장 난도 높은 단계다. 단말에서 실제 소리가 나와야 한다.

**4단계 — 방송국→서버 인제스트.** 군포 앱의 마이크 캡처와 `/ingest` 전송을 구현하고, 3단계에서 완성한 팬아웃 파이프라인에 연결한다. 인제스트 인증 방식도 이 단계에서 확정한다.

**5단계 — 네이티브 앱 UI 통합.** 라이브 제어, 파일 업로드, 방송 트리거, 상태 모니터링을 하나의 앱으로 통합한다. 4단계와 밀접해 사실상 병행한다.

**6단계 — 신뢰성·운영.** WSS heartbeat, 재접속 복구, soak 테스트, systemd 등록, certbot 자동 갱신 훅, 표준 로그 정리.

---

## 12. 주요 설계 결정 요약

군포 송출은 네이티브 앱 하나로 통합한다. 라이브·파일·상태를 한 프로그램이 모두 담당해 방송 상태가 한 곳에서 관리되고, 배포 파이프라인과 서버 연결·인증이 일관된다. 절충안(라이브는 앱, 관리는 웹)은 상태 분산과 이중 배포 문제로 채택하지 않았다.

마이크 인코딩은 군포 앱에서 FFmpeg으로 직접 수행해 서버 변환 부담을 없앤다. 서버는 UI를 갖지 않고 채널과 API만 제공한다. 배포는 GitHub Actions 수동 트리거를 기본으로 한다. 모니터링은 Kafka 없이 journald·Lightsail 콘솔·내부 카운터·`/api/health`로 가볍게 구성한다. TLS는 Let's Encrypt를 쓰며 도메인 구매가 전제이고, C6 펌웨어가 ISRG Root X1을 신뢰해야 한다.

사양 문서 `00`~`08`은 서버↔단말 인터페이스의 기준이다. 과거 데모 코드는 전체 흐름 파악용 참고일 뿐이며 실제 구현 기준으로 삼지 않는다.