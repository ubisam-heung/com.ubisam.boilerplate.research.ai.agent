# ai-agent — 하이브리드 코딩 에이전트

작업 설명을 받아 **로컬 LLM(Ollama)** 으로 작은 작업을 처리하고, 크고 복잡한 작업은
**외부 도구(Claude Code / Codex CLI)** 로 위임하는 하네스 기반 코딩 에이전트입니다.

이 README 하나만 보고 처음부터 끝까지 설치·실행할 수 있도록 작성했습니다.

> **시작점은 사용하는 OS에 따라 다릅니다.**
>
> - **Windows** — 먼저 WSL 2 Ubuntu가 필요합니다. 아래 [`## 0. Windows 사전 준비 (WSL 2)`](#0-windows-사전-준비-wsl-2)를 끝낸 뒤 `## 1. 사전 준비`로 이어집니다.
> - **macOS / Linux (또는 이미 WSL Ubuntu 안)** — 바로 [`## 1. 사전 준비`](#1-사전-준비)부터 진행하면 됩니다.

---

## 무엇인가 / 무엇이 아닌가

- **이다**: ollama로 직접 도는 **독립 실행형 에이전트**. 관련 파일 탐색 → 라우팅 판단 →
  (로컬) 계획·변경·검증·자동 복구 / (외부) Claude Code·Codex 위임까지 자체 파이프라인으로 수행합니다.
- **아니다**: 단순 프롬프트 래퍼가 아닙니다. 검증(`verify_commands`)과 백업·자동 복구, 가드레일 hook을 포함합니다.

### 동작 흐름

```
입력
 └─> 관련 파일 탐색 (로컬 LLM)
      └─> 라우팅 판단 (local | external)
           ├─ [local]    계획 수립 → diff 생성/적용 → 검증 → 실패 시 자동 복구(최대 N회)
           └─ [external]  Claude Code / Codex CLI 호출
```

라우팅은 자동(`auto`)이 기본이며, 대화형 모드에서 `/model` 명령으로 `local`·`claude`·`codex`를 강제할 수도 있습니다.

---

## 0. Windows 사전 준비 (WSL 2)

Windows에서는 Linux 환경(WSL 2 Ubuntu) 안에서 실행하는 것을 전제로 합니다.

PowerShell을 **관리자 권한**으로 열고:

```powershell
wsl --install -d Ubuntu
```

설치 후 재부팅하고, 시작 메뉴에서 **Ubuntu**를 실행해 최초 사용자 계정을 만듭니다.
이후 모든 명령은 **Ubuntu 터미널 안에서** 실행합니다. 그런 다음 아래 `## 1. 사전 준비`(Linux 경로)를 그대로 따라가세요.

---

## 1. 사전 준비

필요한 것은 ① Python 3.10+, ② Ollama + 모델 2종, ③ (선택) Claude Code / Codex CLI 입니다.

### 1-1. 기본 도구

**Linux / WSL Ubuntu**

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip
```

**macOS** (Homebrew 필요 — 없으면 https://brew.sh)

```bash
brew install git python
```

Python 버전 확인 (3.10 이상이어야 함):

```bash
python3 --version
```

### 1-2. Ollama 설치 및 모델 다운로드 
#### (선택 - 현재 조흥재 연구원의 맥북을 기반으로 설정되어있음 | "ubisam" WIFI 연결필요)

**Linux / WSL Ubuntu**

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**macOS**

```bash
brew install ollama
brew services start ollama   # 백그라운드 서버 자동 실행
```

서버 실행 (Linux/WSL은 별도 터미널에서 띄워 둡니다. macOS는 위 brew services로 이미 실행 중):

```bash
ollama serve
```

> `address already in use` 가 뜨면 이미 실행 중이라는 뜻이니 그대로 두면 됩니다.

모델 1종을 받습니다. 메인 코딩 모델입니다:

```bash
ollama pull qwen2.5-coder:7b   # 메인 코딩 모델
```

> **메모리 권장**: 7B Q4 모델 기준 RAM 16GB 이상을 권장합니다. 사양이 낮으면
> `config.yaml`의 `local_llm.model`을 더 작은 모델로 바꾸세요.

설치 확인:

```bash
curl http://localhost:11434/api/tags
```

### 1-3. 외부 도구 — Claude Code / Codex CLI

`auto` 라우팅에서 큰 작업이 외부로 위임되거나, `/model claude` · `/model codex`를
쓰려면 해당 CLI가 PATH에 있어야 합니다. 로컬(`local`)만 쓸 거라면 건너뛰어도 됩니다.

**Codex CLI** (Node.js LTS 필요):

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
. "$HOME/.nvm/nvm.sh"
nvm install --lts
npm i -g @openai/codex
codex --version
```

**Claude Code**:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

각 CLI는 **처음 실행할 때 인증 흐름이 자동으로 시작**됩니다. 아래 절차대로 한 번 로그인해 두면 이후에는 자동으로 인증이 유지됩니다.

**Claude Code 로그인**

```bash
claude
```

처음 실행하면 약관 동의 후 아래 두 가지 중 하나를 선택합니다.

- **Claude.ai 계정으로 로그인 (OAuth)** — 브라우저가 열리며 claude.ai 계정으로 로그인하면 토큰이 자동 저장됩니다. Claude Pro / Max 구독 플랜이 필요합니다.
- **API 키 입력** — Anthropic Console([console.anthropic.com](https://console.anthropic.com))에서 발급한 키를 붙여넣습니다. API 키 과금은 사용량 기반입니다.

인증이 완료되면 `~/.claude/` 디렉토리에 자격 증명이 저장되며, 이후에는 `claude` 실행 시 자동 로그인됩니다.

**Codex CLI 로그인**

```bash
codex
```

처음 실행하면 OpenAI API 키를 묻습니다. [platform.openai.com/api-keys](https://platform.openai.com/api-keys)에서 발급한 키를 입력하세요. 입력한 키는 `~/.codex/config.toml`에 저장됩니다.

---

## 2. 설치

저장소를 클론하고 `install.sh`로 **대상 프로젝트 디렉토리**에 프레임워크를 설치합니다.
대상 경로는 혼동을 막기 위해 절대경로 또는 `~/...` 형태를 권장합니다.

```bash
git clone <이 저장소 URL>
cd ai-agent

./install.sh --target ~/demo-agent-project --tool all
```

`install.sh`가 하는 일:

- 프레임워크 파일 복사 (`agent.py`, `cli.py`, `router.py`, `config.yaml`, `backends/`, `harness/`, `src/`, `AGENTS.md`, `doctor.sh`)
- `.venv` 가상환경 생성 + `requirements.txt` 의존성 설치
- `memory/`, `workspace/`, `.env`, `.gitignore` 생성
- `./agent` 실행 파일 생성

> **중요 — 작업 폴더 구조**: 프레임워크 파일(`agent.py` 등)과 수정 대상 코드를 분리합니다.
> 에이전트는 **`workspace/` 안의 파일만** 탐색·수정합니다. 작업할 프로젝트가 git 저장소라면
> 에이전트 설치 후 `workspace/` 안에서 `git clone`하거나, 기존 코드를 그대로 복사해 두세요.
>
> ```bash
> cd ~/demo-agent-project/workspace
> git clone <프로젝트 저장소 URL>
> ```
>
> 대상 폴더는 `config.yaml`의 `harness.work_dir`로 바꿀 수 있습니다(`"."`로 두면 루트 전체).

`--tool` 옵션 — 외부 위임 시 기본으로 쓸 도구(`config.yaml`의 `external_tools.default`)를 정합니다:

| 값 | 의미 |
|---|---|
| `all` | 기본값. Claude Code를 기본 외부 도구로 사용 |
| `claude` | 외부 위임 시 Claude Code 사용 |
| `codex` | 외부 위임 시 Codex 사용 |
| `local` | 로컬 우선. 외부 위임 시엔 Claude Code 사용 |

예시:

```bash
./install.sh --target ~/my-project --tool codex
./install.sh --target ~/my-project --tool claude
```

---

## 3. 설치 점검 (doctor.sh)

설치한 프로젝트 상태를 진단합니다. Python·가상환경·파일 구조·패키지·Ollama·모델 연결을 한 번에 확인합니다.

```bash
cd ~/demo-agent-project
./doctor.sh .
```

또는 클론한 저장소에서 대상 경로를 지정해도 됩니다:

```bash
./doctor.sh ~/demo-agent-project
```

모든 항목이 `✓`로 통과하면 준비 완료입니다. Ollama 항목이 `✗`면 `ollama serve`가 실행 중인지 확인하세요.

---

## 4. 실행

Ollama가 실행 중인 상태에서, 설치한 프로젝트 디렉토리로 이동해 `./agent`를 실행합니다.
수정할 코드는 미리 `workspace/`에 넣어 둡니다.

```bash
cd ~/demo-agent-project
cp -r ~/my-existing-project/* workspace/   # 예: 기존 코드를 작업 폴더에 복사
```

### 대화형 모드

```bash
./agent
```

프롬프트가 뜨면 자연어로 작업을 입력합니다:

```
agent (auto) ❯ user.py에 이메일 형식 검증 함수를 추가해줘
```

괄호 안(`auto`)은 현재 선택된 실행 모델입니다.

### 단일 작업 모드

대화형 없이 한 번만 실행:

```bash
./agent "user.py에 이메일 형식 검증 함수를 추가해줘"
```

### 질문 / 설명 (수정 없이 읽기)

"설명해줘", "분석해줘", "무슨 코드야" 같은 **질문형 입력**은 코드를 수정하지 않고
읽어서 답하는 **설명 모드**로 처리됩니다(로컬 LLM 사용).

```
agent (local) ❯ Calculator/main.py가 무슨 코드인지 설명해줘
```

- "추가/수정/리팩토링" 같은 수정 키워드가 함께 있으면 일반 수정 작업으로 처리됩니다.
- 더 깊은 분석이 필요하면 `/model claude` 로 위임하세요.

### 대화형 명령어

| 명령어 | 설명 |
|---|---|
| `/help` | 도움말 |
| `/model` | 실행 모델 선택 (아래 참고) |
| `/status` | Ollama·모델 연결 상태 확인 |
| `/config` | 현재 `config.yaml` 설정 보기 |
| `/metrics` | 작업 지표 요약 (로컬처리율·검증/복구율·비용절감) |
| `/history` | 최근 작업 목록 |
| `/clear` | 화면 지우기 |
| `/exit` | 종료 |

---

## 5. 실행 모델 선택 (/model)

대화형 모드에서 작업을 어떤 백엔드로 처리할지 직접 고를 수 있습니다.
선택한 모델은 프롬프트에 `agent (claude) ❯` 처럼 표시됩니다.

```
agent (auto) ❯ /model claude
  ✓  실행 모델을 claude 로 설정했습니다.
agent (claude) ❯
```

| 값 | 동작 |
|---|---|
| `auto` | 기본. 라우터가 작업 크기를 보고 local/external 자동 판단 |
| `local` | 라우팅을 건너뛰고 **로컬 LLM 강제** (Ollama 필요) |
| `claude` | **진짜 Claude Code 대화형 세션으로 전환** (TTY 핸드오프) |
| `codex` | **진짜 Codex 대화형 세션으로 전환** (TTY 핸드오프) |

- 인자 없이 `/model` 만 입력하면 현재 모델과 선택지를 보여줍니다.
- `local`은 로컬 LLM이라 Ollama가 필요하고, `claude`/`codex`는 Ollama 없이도 동작합니다.

### claude / codex 대화형 핸드오프

`/model claude` 또는 `/model codex` 상태에서 작업을 입력하면, 비대화형 1회성 호출이 아니라
**현재 터미널을 진짜 `claude`/`codex` 대화형 세션으로 넘깁니다.** 입력한 작업이 첫 메시지로 전달됩니다.

```
agent (claude) ❯ myFront.vue에 검색 버튼 1개만 추가해줘
  ── claude 대화형 세션 ──
  (진짜 claude TUI — 권한 승인·후속 대화가 그대로 동작)
  > ...
  ── claude 세션 종료 — agent로 복귀 ──
agent (claude) ❯
```

- 권한 승인, 대화 연속성이 claude/codex 네이티브로 동작합니다.
- 세션을 종료(`exit` · `Ctrl-D`)하면 다시 agent 프롬프트로 돌아옵니다.
- 작업은 `workspace/`(= `harness.work_dir`)에서 실행됩니다.
- 대화형 실행 명령은 `config.yaml`의 `external_tools.*.interactive_command`로 바꿀 수 있습니다.

> 참고: 자동 라우팅(`auto`)에서 외부로 위임될 때는 기존처럼 비대화형 1회성(`-p`/`exec`)으로 호출됩니다.
> 대화형 세션이 필요하면 `/model claude`처럼 명시적으로 선택하세요.

---

## 5-1. 작업 지표 (metrics)

매 작업 실행이 `logs/metrics.jsonl`에 자동 기록됩니다. 누적된 지표는 다음으로 확인합니다:

```bash
./agent          # 대화형에서  /metrics
python metrics.py [프로젝트 디렉토리]   # 스탠드얼론 리포트
```

출력 예시:

```
AI Agent 작업 지표 (기대효과 정량화)
  총 작업 건수            7건
  로컬 처리 비율          67%  (로컬 4 / 외부 2)   ← 사내 코드 외부 미전송 비율
  평균 처리시간           30.8초
  검증 통과율             67%
  자동복구 성공률         50%
  외부 미전송 토큰(추정)  5,900 tok
  추정 비용 절감          $0.02  (@$3.0/1M tok)
```

- **로컬 처리 비율** = 라우팅된 작업 중 로컬에서 처리된 비율(= 사내 코드를 외부로 보내지 않은 비율).
- 외부 LLM 단가는 `harness/metrics.py`의 `DEFAULT_PRICE_PER_MTOK`로 조정합니다.
- 보안형 하이브리드 Agent의 **정량 효과(보안·비용)** 를 그대로 보여주는 발표용 지표입니다.

---

## 6. 설정 (config.yaml)

설치된 프로젝트의 `config.yaml` 한 곳에서 모든 동작을 조정합니다.

| 키 | 설명 |
|---|---|
| `local_llm.model` | 메인 코딩 모델 (기본 `qwen2.5-coder:7b`) |
| `local_llm.router_model` | 라우팅 판단용 경량 모델 (기본 `qwen2.5:3b`) |
| `local_llm.base_url` | Ollama 주소 (기본 `http://localhost:11434`) |
| `routing.max_local_files` | 이 파일 수를 넘으면 외부로 라우팅 |
| `routing.max_local_tokens` | 이 토큰 수를 넘으면 외부로 라우팅 |
| `routing.force_external_keywords` | 포함 시 무조건 외부로 보내는 키워드 (예: "전체 리팩토링") |
| `external_tools.default` | 기본 외부 도구 (`claude_code` \| `codex`) |
| `external_tools.claude_code.command` | Claude Code 실행 명령 (기본 `["claude", "-p"]`) |
| `external_tools.codex.command` | Codex 실행 명령 (기본 `["codex", "exec"]`) |
| `harness.max_recovery_retries` | 검증 실패 시 자동 복구 재시도 횟수 (기본 3) |
| `harness.verify_timeout_sec` | 검증 명령 타임아웃 초 (기본 120) |
| `harness.backup_dir` | 변경 전 백업 디렉토리 (기본 `.agent_backup`) |
| `harness.exclude_dirs` | 파일 탐색에서 제외할 디렉토리 |

외부 CLI의 비대화형 실행 옵션이 다르면 `external_tools`의 `command` 값을 수정하세요.

---

## 7. 가드레일 / 안전장치

작업 전·중·후에 다음 보호 장치가 동작합니다. 행동 규칙 전문은 [AGENTS.md](AGENTS.md) 참고.

- **변경 전 백업** — 모든 수정 전 원본을 `.agent_backup/`에 백업합니다.
- **자동 복구** — 검증(`verify_commands`) 실패 시 최대 N회 자동 복구, 그래도 실패하면 백업 경로를 안내합니다.
- **Hook 기반 차단** (`.agent-harness/hooks/`):
  - `pre-bash` — `rm -rf`, `git reset --hard` / `clean -f` / force push, `git config user.*` 변경,
    `sudo`·`shutdown`·`mkfs`·`dd` 등 시스템 변경 명령 차단
  - `pre-file` — `.env`, SSH 키, 클라우드 credential 등 민감 파일 접근 차단
  - `post-edit` — Python 파일 수정 후 `ruff`가 있으면 `ruff check` 실행

### 프로젝트별 hook

Hook은 **두 곳**에서 찾아 모두 실행합니다 (하나라도 차단하면 차단):

| 위치 | 역할 |
|---|---|
| `<프레임워크 루트>/.agent-harness/hooks/` | 공통 가드레일 (설치 시 기본 제공) |
| `workspace/.agent-harness/hooks/` | **이 프로젝트 전용 hook** (직접 추가) |

프로젝트 고유 규칙을 넣으려면 `workspace/.agent-harness/hooks/`에 `pre-bash.sh` / `pre-file.sh` /
`post-edit.sh`를 만들면 됩니다. 인자 1개(명령어 또는 파일경로)를 받아 **차단 시 비-0 종료**하면 됩니다.

**예시 — 이 프로젝트에서만 `npm publish` 금지** (`workspace/.agent-harness/hooks/pre-bash.sh`):

```bash
#!/usr/bin/env bash
CMD="${1:-}"
if echo "$CMD" | grep -q "npm publish"; then
    echo "[BLOCKED] 이 프로젝트에서는 npm publish 금지" >&2
    exit 1   # 비-0 → 차단
fi
exit 0       # 0 → 통과
```

- 별도 등록·설정 없이 파일만 두면 자동으로 인식됩니다(`bash`로 실행되므로 `chmod +x`는 선택).
- `post-edit.sh`는 차단용이 아니라 후처리용(예: 린트)이라 종료코드와 무관하게 출력만 사용됩니다.

- **권장** — git 저장소라면 `git status`가 깨끗한 상태에서 실행하세요. 필요 시 `git checkout -- <file>`로 수동 복구할 수 있습니다.

### claude / codex 핸드오프용 hook · skill · mcp

로컬 파이프라인에는 **skill / mcp 개념이 없습니다.** 이들은 `/model claude`·`codex`로 핸드오프해
**진짜 CLI로 작업할 때** 그 CLI의 표준 설정으로 적용합니다. 핸드오프는 `workspace/`를 작업 폴더로
실행하므로, 설정도 `workspace/`(또는 그 안 repo 루트) 기준으로 둡니다.

**Claude Code** (`/model claude`)

| 종류 | 위치 | 예시 |
|---|---|---|
| 지시문 | `workspace/CLAUDE.md` | 프로젝트 규칙·구조 |
| hook | `workspace/.claude/settings.json` 의 `hooks` | PreToolUse / PostToolUse 등 |
| skill | `workspace/.claude/skills/<이름>/SKILL.md` | frontmatter에 `name`·`description` |
| mcp | `workspace/.mcp.json` 또는 `claude mcp add` | 아래 예시 |

```jsonc
// workspace/.mcp.json
{
  "mcpServers": {
    "playwright": { "command": "npx", "args": ["-y", "@playwright/mcp@latest"] }
  }
}
```

**Codex** (`/model codex`)

| 종류 | 위치 | 예시 |
|---|---|---|
| 지시문 | `workspace/AGENTS.md` | 프로젝트 규칙 |
| mcp | `~/.codex/config.toml` 의 `[mcp_servers.*]` | 아래 예시 |

```toml
# ~/.codex/config.toml
[mcp_servers.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest"]
```

> skill은 Claude Code 고유 기능이라 Codex에는 동일 개념이 없습니다(Codex는 `~/.codex/prompts/`의
> 커스텀 프롬프트로 대체). 각 CLI의 정확한 스키마는 `claude --help` / `codex --help` 및 공식 문서를 확인하세요.

---

## 8. 디렉토리 구조

설치된 프로젝트는 대략 다음과 같이 구성됩니다.

```text
demo-agent-project/          ← 프레임워크 (바깥)
├── agent              # 실행 런처 (./agent)
├── agent.py           # 메인 진입점 (run_agent)
├── cli.py             # 대화형 TUI
├── router.py          # local / external 라우팅
├── config.yaml        # 설정 한 곳 (harness.work_dir 포함)
├── doctor.sh          # 진단 스크립트
├── AGENTS.md          # 에이전트 행동 규칙(가드레일)
├── requirements.txt
├── backends/          # LLM·외부 도구 어댑터 (local_llm / claude_code_cli / codex_cli)
├── harness/           # 실행 파이프라인 (context/planner/executor/verifier/recovery/hooks)
├── src/               # 프레임워크 공용 유틸
├── memory/            # 에이전트 메모리
├── workspace/         # ★ 수정 대상 프로젝트 (안쪽) — 에이전트는 여기만 본다
│   └── (각자 만든 main.py, user.py 등)
└── .venv/             # 가상환경
```

프레임워크 코드와 작업 코드가 분리되므로, 에이전트가 자기 자신(`agent.py` 등)을 건드리거나
탐색 대상에 섞이는 일이 없습니다.

> 소스 저장소에는 코드 구조 지도 [CLAUDE.md](CLAUDE.md)와 가드레일 hook(`.agent-harness/hooks/`)도 포함됩니다.

---

## 9. 동작 확인 체크리스트

1. `ollama serve` (또는 macOS `brew services list`)로 Ollama 실행 확인
2. `curl http://localhost:11434/api/tags` 로 모델 목록 확인
3. `./doctor.sh .` 전체 `✓` 확인
4. 작은 작업으로 먼저 테스트 (예: 단일 함수 추가)
5. `.agent_backup/`에 변경 전 파일이 백업되는지 확인
6. 큰 작업("전체 리팩토링" 등)을 입력해 외부 도구로 라우팅되는지 확인

---

## 10. 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `Ollama 서버에 연결할 수 없습니다` | `ollama serve` 미실행. 별도 터미널에서 실행 |
| `ollama serve` → `address already in use` | 이미 실행 중. 정상이므로 무시 |
| `/model claude` 가 동작 안 함 | `claude` CLI 미설치 또는 미로그인. [1-3](#1-3-선택-외부-도구--claude-code--codex-cli) 참고 |
| 모델이 너무 느림 / 메모리 부족 | `config.yaml`의 `local_llm.model`을 더 작은 모델로 변경 |
| `python3: command not found` (WSL) | `sudo apt install -y python3 python3-venv python3-pip` |
| 외부 CLI 옵션 오류 | `config.yaml`의 `external_tools.*.command` 수정 |

---

