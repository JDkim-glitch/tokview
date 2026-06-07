# 2026-06-06 — Usage panel deep-dive, event polling, copy/logo UX

이 문서는 본 세션에서 진행한 작업을 다음 세션이 컨텍스트 없이 이어받을 수
있도록 아카이브한 기록이다. 작업 디렉터리는
`/Users/jdethankim/ClaudeCode/tokview`. 시작 시점 HEAD는 `ebecbcf`이며 변경분은
아직 커밋되지 않았다 (`git diff --stat`: 6 files, +813 -30).

## 한 줄 요약

오른쪽 Usage 패널이 사용 중 모델·세션 진행·일/주 리셋·총합을 agent별로 모두
보여주도록 깊이 확장했고, 이벤트 기반 폴링으로 hermes는 ~1ms·claude는 ~770ms
응답 속도를 달성했다. 사이드바 하단에 실제 agent 로고를 띄우고, 마우스
드래그로 PTY 화면을 선택하면 자동으로 클립보드에 복사된다.

## 변경 파일 요약

| 파일 | 변경 요지 |
|---|---|
| `tokview/usage_poller.py` | hermes `state.db` 직접 리더, codex 다른 ccusage 스키마 분기, 30초 캐시, 일/주 리셋 계산, `UsageSnapshot` 필드 확장 |
| `tokview/usage_panel.py` | Model 섹션, Session 섹션(hermes 라이브), Today에 리셋 카운트다운 + cache + 4자리 비용 |
| `tokview/pty_terminal.py` | Activity 메시지(Enter/idle), 마우스 드래그 선택, OSC52/pbcopy 복사, Ctrl+Shift+C, 한글 wide-char 렌더 보정, bracketed paste 포워딩 |
| `tokview/app.py` | `on_pty_terminal_widget_activity` → poller poke, 사이드바 로고 갱신 호출 |
| `tokview/sidebar.py` | 하단 `#sidebar-logo` 영역 신설, agent별 실제 로고 매핑, claude/hermes는 실제 부팅 배너 형태 |
| `tokview/status_bar.py` | 단일 1행 단축키 텍스트로 유지 (로고는 사이드바로 이동) |

## 사용자 요구 — 흐름

세션을 따라 사용자가 요청한 항목을 순서대로 정리한다.

1. **agent별 모델·사용량을 우측 패널에 표시.** hermes 설정에서 모델·프로바이더를
   읽고, ccusage daily의 `modelsUsed`/`messageCount`를 surface하라.
2. **agent별 무료 한도·리셋 시점도 표시. 실시간 반영 가능한가.**
   ccusage 능력 차이(claude만 weekly/blocks)와 각 플랫폼 리셋 규칙을 정리한 뒤
   사용자가 "1번 범위(전 agent 공통+리셋) + hermes/claude 두 개는 깊이"와
   "이벤트 기반 폴링"을 선택.
3. **계정별 격리 확인.** "OS user 단위로만 자동 분리, agent CLI 안의 로그인
   계정별로는 분리 안 됨"을 설명. ccusage에는 `--user` 필터 자체가 없음.
4. **GitHub redistribution 시 무용한가.** 아니다 — 각자 자기 머신의 `~/.{agent}/`
   를 읽으니 본인 데이터만 보인다.
5. **단축키 옆에 활성 agent 로고 표시.** 처음엔 StatusBar를 5행으로 확장했다가,
6. **로고는 status bar 아닌 좌측 사이드바 하단에. 그리고 복사가 안 된다.**
   StatusBar 1행 환원 → 사이드바에 `#sidebar-logo` 추가, PtyTerminalWidget에
   드래그 선택+클립보드 복사 구현.
7. **Claude 실제 로고는 `▗ ▖ ▖ ▘▘ ▝▝` 패턴.** 교체 완료.
8. **hermes 실제 로고 직접 캡처해서 교체.** `pty.fork`로 `hermes chat` 부팅 5초
   읽어 ANSI 제거 후 braille 도트 헤르메스 머리 형상 5줄을 추출 → 교체.

## 데이터 계층 (`usage_poller.py`)

### UsageSnapshot 신규 필드

```python
model: str | None           # 현재 모델 (hermes config 또는 modelsUsed[-1])
provider: str | None        # hermes만 사용
models_today: list[str]     # ccusage modelsUsed 또는 codex models dict 키
today_messages: int         # ccusage messageCount, hermes는 SUM(message_count)
session_*: ...              # hermes 활성 세션 라이브(state.db에서)
daily_remaining_min: int    # 자정까지 분 — claude 제외 전 agent
```

### Agent별 폴 경로

```
hermes  → ~/.hermes/state.db (SQLite, ~1ms)
            + ~/.hermes/config.yaml (모델 기본값)
            + ccusage hermes daily (cost만, 30초 캐시)
claude  → ccusage claude daily + weekly + blocks (~770ms)
codex   → ccusage codex daily (costUSD/cachedInputTokens/models dict 스키마)
gemini  → ccusage gemini daily
copilot → ccusage copilot daily
```

state.db는 cost가 sparse(estimated=0/actual=NULL이 대부분)라서 cost는 ccusage가
출처. 그래서 hermes도 ccusage를 함께 부르지만 `_get_ccusage_cached`로 30초
캐싱 → 사용자가 Enter 연타해도 매번 1.7s 안 기다림.

### `_merge_daily`의 스키마 분기

ccusage가 agent별로 키 이름이 다르다. 매핑:

| 의미 | claude/hermes | codex |
|---|---|---|
| 비용 | `totalCost` | `costUSD` |
| 캐시 read | `cacheReadTokens` | `cachedInputTokens` |
| 모델 목록 | `modelsUsed: []` | `models: {name: {...}}` |
| 메시지 수 | `messageCount` | (없음) |

`is_codex = agent == "codex"`로 분기, helper(`cost_of`/`cache_read_of`/`models_of`)가
양쪽을 흡수.

### 리셋 계산

```python
next_weekly_reset(now) -> next Mon 09:00 local      # claude만
next_daily_reset(now)  -> tomorrow 00:00 local      # 그 외 전부
```

claude는 weekly+block, 그 외는 daily만. ccusage daily 버킷이 로컬 자정 기준이라
"내일 00:00"이 자연스러운 기준.

## 패널 레이아웃 (`usage_panel.py`)

세션이 hermes일 때 실제 출력 예 (실측 30컬럼):

```
Usage · hermes
────────────────────
Model
nvidia/nemotron-3-ultra:free
via nous
Also today: gpt-5.3-codex

Session · since 14:40
Current Model Informa…
In:    17,996
Out:   86
Msgs:  2
Calls: 1

Today 06-06 · 8h 24m left
In:    17,996
Out:   86
Msgs:  3
Cost:  $0.00
Resets 00:00

All time
In:    401,508
Out:   18,312
Cost:  $1.28
```

claude는 Model + Block(5h, quota bar, burn, proj) + Today + Week + All time.
codex/gemini/copilot은 Model(있으면) + Today(0,리셋포함) + All time.

## 이벤트 기반 폴링 (`pty_terminal.py` + `app.py`)

`PtyTerminalWidget`이 두 종류의 `Activity` 메시지를 발행:

| Kind | 트리거 |
|---|---|
| `submit` | Enter 키 송신, 또는 paste 텍스트에 `\n`/`\r` 포함 |
| `output` | PTY 청크 수신 후 500ms idle (`OUTPUT_IDLE_DELAY`) |

`TokviewApp.on_pty_terminal_widget_activity`가 활성 세션 활동에 한해
`poller.poke()`. 5초 fallback 폴은 그대로 살아있어서 비활성 세션의 cost
업데이트도 누락 안 됨.

실측 속도:
- hermes 콜드: 1793ms (ccusage 첫 호출 포함)
- hermes 웜: **1.4ms** (state.db만, ccusage 30s 캐시)
- claude: 770ms (ccusage 3회: daily+weekly+blocks)
- codex/gemini/copilot: 200~285ms

## 사이드바 로고 (`sidebar.py`)

`#sidebar-logo` Static 위젯, `height: 7`, `content-align: center middle`.
세션 없을 땐 "no active session / press F2 to start"; 세션 활성화 시
`update_sessions()` 안에서 `_update_logo(active_agent)` 호출.

agent별 로고:

| Agent | 색 | 형태 |
|---|---|---|
| claude | dark_orange | 실제 부팅 로고 `▗ ▖ ▖` / `▘▘ ▝▝` + "Claude Code · Opus 4.7" |
| hermes | medium_purple | 실제 부팅 배너 braille 헤르메스 머리 5줄 + "Hermes Agent · Nous" |
| codex | spring_green3 | 자작 block-letter "CODEX" |
| gemini | deep_sky_blue1 | 자작 block-letter "GEMINI" |
| copilot | magenta2 | 자작 block-letter "COPILOT" |
| bash | green | 자작 block-letter "BASH" |

claude/hermes는 진짜 배너에서 캡처 ─ hermes는 `pty.fork`로
`hermes chat` 5초 부팅 캡처 후 braille 도트 아트 5줄 추출 (`/tmp/capture_hermes2.py`
스크립트 사용). 다른 agent도 실제 배너로 교체할 여지 있음 — 사용자가 텍스트
붙여주거나 같은 방식으로 캡처하면 됨.

## 복사 기능 (`pty_terminal.py`)

Textual이 마우스를 캡처해 호스트 터미널의 네이티브 드래그 선택이 안 되던
문제를 자체 선택+복사로 해결.

- `on_mouse_down(button=1)`: anchor=cursor=(col,row), `capture_mouse()`, refresh
- `on_mouse_move`: cursor 갱신, 선택 영역에 reverse video 적용
- `on_mouse_up`: 단순 클릭이면 선택 해제, 드래그면 텍스트 추출 후 클립보드
- `Ctrl+Shift+C`: 기존 선택이 있으면 강제 복사
- 선택 텍스트는 `_in_linear_selection` 스트림식 (시작행 끝까지 → 중간행 전체
  → 끝행 시작까지)로 추출, 행마다 trailing space 제거

클립보드 메커니즘 이중화:
1. `App.copy_to_clipboard(text)` — Textual 8.2.7의 OSC 52
   (iTerm2/WezTerm/kitty 즉시 동작, Terminal.app은 환경설정 필요)
2. macOS는 추가로 `subprocess.run(["pbcopy"], ...)` — 권한 무관 항상 동작

선택 영역 `cell.reverse ^ is_cursor ^ is_sel` 로 reverse 토글, pyte의 wide-char
스텁(`data == ""`)은 렌더 건너뛰어 행이 어긋나지 않게.

## 검증 결과

```
$ .venv/bin/python -m py_compile tokview/*.py  # 전체 컴파일 OK
$ .venv/bin/python                            # 폴 latency 실측
hermes: 11.8 ms (state.db 단독, ccusage 캐시 후)
claude: 770.4 ms
codex:  285.1 ms
gemini: 201.2 ms

$ .venv/bin/python                            # 사이드바 로고 헤드리스 부팅
initial logo: "no active session / press F2 to start"
after hermes session: braille 헤르메스 머리 5줄 출력 확인

$ pbcopy 라운드트립: returncode 0, pbpaste 정상 readback
```

## 다음 세션 체크리스트

1. **커밋 안 되어 있음.** 작업 전 `git diff`로 변경 6 파일 확인. 사용자가
   커밋 시점·메시지를 정한다.
2. **codex/gemini/copilot 실제 로고 미적용.** 자작 block-letter 상태. 사용자가
   실제 부팅 화면을 붙여주거나 `pty.fork` 기반 캡처 스크립트를 재사용해서
   교체 가능 (`/tmp/capture_hermes2.py` 패턴 참고).
3. **사이드바 너비.** hermes 브라유 아트는 30컬럼. 사이드바 폭이 20%인데
   터미널 폭 150 미만이면 가장자리 클립. 잘림이 거슬리면
   `tokview/tokview.tcss`의 `Sidebar { width: 20% }`를 25~30%로 올리거나
   로고를 좁게 크롭.
4. **OUTPUT_IDLE_DELAY 500ms.** 사용자가 토큰 스트리밍 중에도 더 빨리 보고
   싶다면 200ms로 낮춰도 됨 — 그만큼 ccusage 호출 빈도 증가. 30초 캐시가
   있어서 비용은 한정적.
5. **`CCUSAGE_REFRESH_SECONDS = 30.0`.** 비용이 자주 바뀌는 paid hermes 모델
   사용 시 짧게 (10초) 조정 가능.
6. **claude 계정 분리 미구현.** 멀티 anthropic 계정 사용자가 요청하면
   `~/.claude/sessions/<id>.json`에서 어떤 식별자가 있는지 재조사 필요.
7. **state.db user_id 일관성 없음.** hermes 세션 중 nous provider는 user_id
   NULL, openai-codex provider는 채워짐. 진짜 격리는 provider 단위 필터가
   현실적 (`WHERE billing_provider = ?`).

## 참고 코드 위치

- ccusage daily 스키마 분기: `usage_poller.py` `_merge_daily` 안의
  `cost_of/cache_read_of/models_of` 클로저
- hermes SQLite 쿼리: `usage_poller.py` `read_hermes_state`
- 30초 캐시: `usage_poller.py` `_get_ccusage_cached`
- Activity 메시지: `pty_terminal.py` `PtyTerminalWidget.Activity` 클래스
- 마우스 선택: `pty_terminal.py` `on_mouse_{down,move,up}` + `_selection_text`
- 사이드바 로고 매핑: `sidebar.py` `_LOGOS` 딕셔너리
