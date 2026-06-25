# 07. LIVE Opus 송신 pacing 및 제한형 burst 정책

## 1. 목적

이 문서는 서버가 LIVE Opus frame을 C6로 보낼 때 적용해야 하는 송신 간격, 지연 회복, frame 폐기 기준을 정의한다.

핵심 목적은 다음과 같다.

- 평상시에는 `frame_ms`에 맞춰 일정하게 송신한다.
- 서버, OS, TLS/WSS, 파일 I/O 등으로 송신이 잠깐 늦어진 경우 P4 버퍼가 회복될 수 있도록 제한형 burst를 허용한다.
- P4/C6가 감당하지 못할 정도의 무제한 burst는 금지한다.
- 2초를 초과한 늦은 음성은 실시간성을 위해 폐기한다.

## 2. 기본 원칙

- 서버는 기본적으로 AUDIO WSS로 Opus frame을 `frame_ms` 간격에 맞춰 1개씩 전송한다.
- AUDIO WSS binary message 1개는 Opus frame 1개를 의미한다.
- 여러 Opus frame을 하나의 WSS binary message에 합쳐 보내면 안 된다.
- burst는 여러 개의 WSS binary message를 짧은 간격으로 연속 전송하는 것을 의미한다.
- burst는 평상시 송신 방식이 아니라, 지연 회복을 위한 제한형 catch-up 동작이다.

## 3. 현재 P4/C6 기준값

현재 P4 live audio 기준 설정은 다음을 기준으로 한다.

| 항목 | 값 | 40 ms frame 기준 |
| --- | ---: | ---: |
| `CONFIG_APP_LIVE_MIN_BUFFER_FRAMES` | `20` | `800 ms` |
| `CONFIG_APP_LIVE_REBUFFER_FRAMES` | `8` | `320 ms` |
| `CONFIG_APP_LIVE_JITTER_SLOTS` | `48` | `1920 ms` |
| `CONFIG_APP_LIVE_PACKET_QUEUE_LEN` | `96` | queue 여유 |
| `CONFIG_APP_LIVE_PCM_PREFILL_FRAMES` | `8` | `320 ms` |
| `CONFIG_APP_LIVE_PCM_LOW_WATER_FRAMES` | `1` | `40 ms` |

C6/P4 전송 한계는 다음을 기준으로 한다.

| 항목 | 값 |
| --- | ---: |
| SDIO RPC payload max | `4096 B` |
| LIVE_FRAME header | `12 B` |
| LIVE_FRAME Opus payload max | `4084 B` |
| C6 AUDIO WSS buffer | `4096 B` |

서버는 Opus payload를 `4000 B` 이하로 유지하는 것을 권장한다.

현재 `iot_radio_test_server.py` 구현 상태는 다음과 같다.

- `frame_ms` 기준 cadence 송신은 구현되어 있다.
- 송신이 조금 늦어진 경우 `sleep` 없이 다음 frame을 바로 보내는 자연 catch-up은 가능하다.
- `sleep_for < -0.2s`이면 기준 시각을 현재로 재설정한다.
- 이 문서의 `1600 ms` backlog 보존 상한, `2000 ms` 명시적 drop, P4 `LIVE_STATS` 기반 송신률 보정은 아직 테스트 서버에 완전 구현되어 있지 않다.
- 따라서 이 문서는 현재 코드 전체 구현 설명이 아니라 서버 송신 정책의 목표/권장 기준을 함께 포함한다.

## 4. 서버 backlog 정의

서버는 각 Opus frame의 예정 송신 시각을 계산해야 한다.

```text
expected_send_time = live_start_time + frame_index * frame_ms
```

현재 시각이 예정 송신 시각보다 늦으면 backlog가 발생한 것으로 본다.

```text
backlog_ms = now_ms - expected_send_time_of_next_frame
backlog_frames = backlog_ms / frame_ms
```

backlog가 없으면 정상 pacing으로 보낸다. backlog가 있으면 제한형 burst 또는 오래된 frame 폐기를 적용한다.

## 5. 제한형 burst 기준

P4+C6 현재 구조에서 권장하는 burst 제한은 다음과 같다.

| 항목 | 권장값 |
| --- | ---: |
| 1회 burst group 최대 오디오 시간 | `120 ms` |
| 40 ms frame 기준 1회 최대 frame 수 | `3 frame` |
| 20 ms frame 기준 1회 최대 frame 수 | `6 frame` |
| 1초 평균 송신률 | 정상 속도의 `2배` 이하 |
| 서버 backlog 보존 상한 | `1600 ms` |
| 절대 폐기 기준 | `2000 ms` 초과 |

즉 40 ms frame 기준으로 서버가 잠깐 밀렸을 때는 최대 3개 frame까지 짧게 연속 전송할 수 있다. 그 뒤에는 다시 pacing하거나 다음 event loop에서 추가 catch-up을 판단한다.

## 6. backlog별 처리 정책

| backlog | 처리 |
| ---: | --- |
| `0 ~ 1 frame` | 정상 pacing |
| `2 ~ 3 frame` | 제한형 catch-up 허용 |
| `4 frame 이상` | catch-up 허용, 단 1회 burst group은 `120 ms` 이하 |
| `1600 ms 초과` | 오래된 frame 폐기 시작 |
| `2000 ms 초과` | 2초 초과 frame은 반드시 폐기 |

서버가 지연된 모든 frame을 반드시 전송하려고 하면 안 된다. 실시간 방송에서는 늦은 음성을 모두 살리는 것보다 현재 방송 시점에 맞추는 것이 우선이다.

## 7. P4 LIVE_STATS 기반 보정

P4는 `LIVE_STATS`로 다음 값을 C6/서버 쪽에 전달할 수 있다.

- `p4_buffer_ms`
- `underrun_count`
- `rx_seq_last`

서버가 이 값을 사용할 수 있으면 다음 기준을 적용한다.

| P4 상태 | 서버 송신 정책 |
| --- | --- |
| `p4_buffer_ms >= 800` | 정상 pacing 우선 |
| `p4_buffer_ms < 800` | catch-up 후보 |
| `p4_buffer_ms <= 320` | 적극 catch-up 후보 |
| `p4_buffer_ms >= 1600` | burst 금지, pacing 또는 오래된 frame drop |
| `underrun_count` 증가 | 최근 backlog/drop/gap 로그 기록 |

단, `LIVE_STATS`는 실시간 제어 신호가 아니라 상태 관측값이다. 서버는 `LIVE_STATS`만 기다리지 말고 자체 송신 backlog도 함께 계산해야 한다.

## 8. frame 폐기 원칙

- 서버 내부 큐에서 `age > 2000 ms`인 Opus frame은 전송하지 않는다.
- `age > 1600 ms`인 Opus frame은 catch-up 대상이 아니라 drop 후보로 본다.
- drop 후에도 서버 내부 frame index 또는 sequence 기준은 계속 증가할 수 있다.
- C6/P4는 gap을 감지할 수 있으며, P4는 PLC 또는 재버퍼링으로 처리한다.
- `LIVE_STOP` 이후 남은 backlog frame은 즉시 폐기한다.

## 9. 금지 사항

- 여러 Opus frame을 하나의 AUDIO WSS binary payload에 합쳐 보내지 않는다.
- P4 buffer를 2초 이상 채우기 위한 무제한 burst를 하지 않는다.
- 서버가 지연된 모든 frame을 강제로 다 보내려고 하면 안 된다.
- `LIVE_STOP` 이후 backlog frame을 계속 보내면 안 된다.
- C6가 frame을 재패킹할 때 Opus payload를 디코드하거나 합치면 안 된다.

## 10. 권장 의사코드

```text
while live_active:
    now = monotonic_ms()

    due_frames = frames whose expected_send_time <= now

    drop frames where age_ms > 2000

    if backlog_ms > 1600:
        drop oldest frames until backlog_ms <= 1600

    if p4_buffer_ms is known and p4_buffer_ms >= 1600:
        send at most 1 due frame
    else:
        burst_audio_ms = min(120, due_backlog_ms)
        burst_frames = max(1, burst_audio_ms / frame_ms)
        burst_frames = clamp(burst_frames, 1, max_burst_frames)

        send up to burst_frames
        each frame must be a separate WSS binary message

    sleep/yield according to normal pacing
```

## 11. 40 ms frame 예시

정상 송신:

```text
t=0ms    frame 1
t=40ms   frame 2
t=80ms   frame 3
t=120ms  frame 4
```

서버가 120 ms 늦어진 경우:

```text
t=120ms  frame 1, frame 2, frame 3을 separate WSS binary message로 연속 전송
t=160ms  frame 4부터 정상 pacing 복귀
```

서버가 2초 이상 늦어진 경우:

```text
2초를 초과한 오래된 frame은 폐기
현재 시점에 가까운 frame부터 pacing 또는 제한형 catch-up
```

## 12. 결론

기존의 "버스트 없음" 정책은 평상시 pacing 정책으로는 맞다. 그러나 서버가 반복적으로 늦어지는 상황까지 burst를 완전히 금지하면 P4 버퍼가 회복될 방법이 없다.

따라서 현재 사양은 다음으로 정리한다.

```text
평상시: non-burst pacing
지연 회복: bounded catch-up burst 허용
보존 한계: 1600 ms 권장
절대 한계: 2000 ms 초과 frame 폐기
전송 단위: WSS binary message 1개 = Opus frame 1개
```
