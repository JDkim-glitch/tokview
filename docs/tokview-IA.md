# tokview — IA (Information Architecture & 구조 설계)

> PRD의 기능을 실제 화면 구조, 컴포넌트, 상태, 데이터 흐름, 코드 모듈로 풀어낸 설계 문서.
> Claude Code가 이 문서를 보고 모듈 단위로 작업할 수 있도록 구성한다.

- 문서 버전: v1.0
- 작성일: 2026-06-04
- 짝 문서: `tokview-PRD.md`

---

## 1. 화면 구조 (3단 레이아웃)

```
┌──────────────┬───────────────────────────────┬──────────────────┐
│  Sidebar     │        Terminal View          │   Usage Panel    │
│  (좌 · 20%)   │        (중앙 · 50%)            │   (우 · 30%)      │
│              │                               │                  │
│  세션 목록     │   활성 세션의 라이브 터미널        │  활성 세션의       │
│              │   (PTY 화면 렌더링)             │  토큰 사용량       │
│  ▸ claude #1 │                               │                  │
│  ▸ hermes #1 │   $ ...                       │  입력 토큰: ...     │
│  ▸ claude #2 │   (커서, 입력 가능)             │  출력 토큰: ...     │
│              │                               │  누적 비용: $...    │
│  [+ 새 세션]   │                               │                  │
├──────────────┴───────────────────────────────┴──────────────────┤
│  Status Bar : 활성 세션명 · 단축키 안내 · 폴링 상태                      │
└──────────────────────────────────────────────────────────────────┘
```

- 비율(20/50/30)은 설정 파일에서 조정 가능하게 한다(Phase 4).
- 하단 Status Bar는 전역 정보(활성 세션, 단축키, 폴링 상태)를 표시한다.

---

## 2. 컴포넌트 트리

```
TokviewApp (Textual App)
├── Header (선택)
├── MainLayout (Horizontal)
│   ├── Sidebar (Vertical)
│   │   ├── SessionList
│   │   │   └── SessionListItem  (× N)   # 세션 1개당 1개
│   │   └── NewSessionButton
│   ├── TerminalView (Container)
│   │   └── PtyTerminalWidget            # 활성 세션의 PTY 화면
│   └── UsagePanel (Vertical)
│       ├── UsageHeader                  # 활성 세션 이름 / 에이전트
│       ├── UsageMetrics                 # 입력/출력/누적/비용
│       └── UsageFooter                  # 마지막 갱신 시각 / 폴링 주기
└── StatusBar
```

### 컴포넌트별 책임

- `SessionList` / `SessionListItem`: 세션 목록 표시, 선택 이벤트 발생, 상태 뱃지 표시.
- `NewSessionButton`: 새 세션 생성 트리거(에이전트 선택 → 세션 추가).
- `PtyTerminalWidget`: 활성 세션의 pyte 화면 버퍼를 렌더링, 키 입력을 PTY로 전달, 리사이즈 처리.
- `UsagePanel`: 활성 세션의 사용량 표시. UsagePoller의 결과를 구독.
- `StatusBar`: 전역 상태/단축키 안내.

---

## 3. 상태 관리 (단일 출처: SessionManager)

모든 화면 전환의 기준이 되는 상태는 `SessionManager` 한 곳에서 관리한다.

### 3.1 데이터 모델

```python
class Session:
    id: str                 # 고유 ID
    title: str              # 표시 이름 (예: "claude #1")
    agent: str              # "claude" | "hermes" | ...
    cwd: str                # 작업 디렉터리 (세션별 사용량 귀속에 사용)
    pty_fd: int             # PTY 파일 디스크립터
    process: Popen          # 실행 중인 에이전트 프로세스
    screen: pyte.Screen     # 화면 버퍼
    stream: pyte.Stream     # 출력 파서
    status: str             # "running" | "idle" | "exited"

class SessionManager:
    sessions: dict[str, Session]
    active_id: str | None

    def create_session(agent, cwd) -> Session
    def switch_to(session_id) -> None     # 활성 세션 변경 → UI에 통지
    def close_session(session_id) -> None
    def active() -> Session | None
```

### 3.2 상태 변경 → UI 반영 규칙

- `active_id`가 바뀌면:
  - `TerminalView`는 새 활성 세션의 `screen`을 렌더링한다.
  - `UsagePanel`은 새 활성 세션의 `agent`/`cwd`를 기준으로 사용량을 다시 가져온다.
  - `SessionList`는 활성 항목을 하이라이트한다.

- Textual의 reactive 속성 또는 메시지(message) 시스템으로 위 통지를 구현한다.

---

## 4. 데이터 흐름

### 4.1 키 입력 흐름

```
키보드 → TokviewApp.on_key
       → 활성 세션 판별 (SessionManager.active())
       → os.write(session.pty_fd, 입력 바이트)
       → 에이전트가 처리
```

### 4.2 화면 출력 흐름

```
PTY read (백그라운드 태스크)
       → session.stream.feed(출력 바이트)   # pyte가 화면 버퍼 갱신
       → 활성 세션이면 PtyTerminalWidget.refresh()
```

### 4.3 세션 전환 흐름 (핵심)

```
SessionListItem 클릭/선택
       → SessionManager.switch_to(id)
       → active_id 변경 통지
       → TerminalView 재렌더링 + UsagePanel 재조회 + SessionList 하이라이트
```

### 4.4 사용량 폴링 흐름

```
UsagePoller (주기 타이머, 예: 5초)
       → 활성 세션의 agent/cwd 확인
       → subprocess: bunx ccusage <agent> <report> --json [--project <cwd>]
       → JSON 파싱
       → UsagePanel 갱신 (마지막 갱신 시각 표기)
```

---

## 5. 키맵 / 인터랙션 (초안)

> Phase 4에서 재정의 가능. 충돌 방지를 위해 접두 키(prefix) 방식 권장.

| 동작 | 키 (초안) |
|------|-----------|
| 새 세션 생성 | `Ctrl+N` |
| 다음 세션 | `Ctrl+]` |
| 이전 세션 | `Ctrl+[` |
| 세션 닫기 | `Ctrl+W` |
| 사이드바 포커스 토글 | `Ctrl+B` |
| 종료 | `Ctrl+Q` |

- 주의: 에이전트(예: claude)도 자체 단축키를 쓰므로, tokview 단축키는 접두 키 뒤에 두거나
  에이전트가 쓰지 않는 조합으로 제한해 충돌을 피한다.

---

## 6. 코드 모듈 구조

```
tokview/
├── .venv/
├── tokview/
│   ├── __init__.py
│   ├── app.py              # TokviewApp (Textual App, 진입점)
│   ├── session.py          # Session, SessionManager
│   ├── pty_terminal.py     # PtyTerminalWidget + PTY 읽기/쓰기/pyte 연동
│   ├── sidebar.py          # Sidebar, SessionList, SessionListItem
│   ├── usage_panel.py      # UsagePanel + UsageMetrics 렌더링
│   ├── usage_poller.py     # ccusage --json 호출 및 파싱
│   ├── status_bar.py       # StatusBar
│   └── config.py           # 설정 로드(레이아웃 비율, 폴링 주기, 테마)
├── tokview.tcss            # Textual CSS (레이아웃/스타일)
├── config.toml             # 사용자 설정
├── requirements.txt
└── README.md
```

### 모듈별 의존 관계

- `app.py`가 모든 위젯을 조립하고 `SessionManager`를 소유한다.
- `pty_terminal.py`는 `session.py`에 의존(세션의 PTY/화면 사용).
- `usage_panel.py`는 `usage_poller.py`의 결과를 구독.
- `sidebar.py`는 `session.py`의 목록/상태를 표시하고 선택 이벤트를 `app.py`로 전달.

---

## 7. 외부 의존성

| 의존성 | 용도 | 비고 |
|--------|------|------|
| Textual | TUI 프레임워크 | `.venv`에 pip 설치 |
| pyte | 터미널 화면 버퍼 파싱 | 이스케이프 시퀀스 → 셀 단위 화면 |
| pty (표준) | 가상 터미널 생성 | Python 표준 라이브러리 |
| ccusage | 토큰 사용량 데이터 | 외부 CLI, `bunx`/`npx`로 호출, JSON 출력 |
| claude / hermes | 호스팅 대상 에이전트 | 사용자 환경에 미리 설치 |

---

## 8. 단계별 산출물 매핑 (PRD Phase ↔ 모듈)

| Phase | 주요 산출 모듈 | 검증 포인트 |
|-------|----------------|-------------|
| Phase 0 | `pty_terminal.py` (단독 검증 스크립트) | claude가 실제 입력·응답되는가 |
| Phase 1 | `app.py`, `tokview.tcss` | 3단 레이아웃 + 메인에 터미널 1개 |
| Phase 2 | `session.py`, `sidebar.py` | 세션 생성·전환이 끊김 없이 되는가 |
| Phase 3 | `usage_poller.py`, `usage_panel.py` | 활성 세션 전환 시 사용량 연동되는가 |
| Phase 4 | `config.py`, `status_bar.py`, `tokview.tcss` | 설정/테마/상태 표시 |

---

## 9. 열린 질문 (개발 중 결정)

- 세션별 사용량 귀속을 cwd(프로젝트) 기준으로 할지, 세션 ID 기준으로 할지 — ccusage의
  `--instances`/`session` 동작을 Phase 3에서 검증 후 확정.
- 사용량 폴링 주기 기본값(3초 / 5초)과 성능 영향.
- PtyTerminalWidget의 리사이즈 시점 처리(터미널 크기 변경을 PTY에 어떻게 전파할지).
