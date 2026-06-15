# AGENTS.md

이 프로젝트에서 작업하는 AI 에이전트(로컬 LLM, Claude Code, Codex)를 위한 공통 지침입니다.

## 기본 원칙

1. **hook을 존중한다**: `.agent-harness/hooks/`의 pre-bash, pre-file, post-edit 검사를 우회하지 않는다.
   - 차단된 명령/파일 접근이 필요하다고 판단되면, 강행하지 말고 사용자에게 보고한다.
2. **사용자 변경을 보존한다**: 관련 없는 파일/코드 스타일을 임의로 수정하지 않는다.
3. **프로젝트에 문서화된 명령을 우선한다**: 테스트/빌드/린트는 `config.yaml`의 `verify_commands` 또는
   프로젝트 표준 명령(`pytest`, `npm test` 등)을 사용한다. 임의의 새 도구를 도입하지 않는다.
4. **검증 결과를 보고한다**: 변경 후 실행한 검증 명령과 결과(성공/실패, 핵심 오류 메시지)를 명확히 보고한다.

## 금지 사항 (hook이 1차로 차단하지만, 에이전트 스스로도 시도하지 말 것)

- `rm -rf` 및 루트/홈 디렉토리 대상 삭제
- `git config user.name` / `user.email` 변경
- `git reset --hard`, `git clean -f*`, force push
- `sudo`, `su`, `shutdown`, `reboot`, `systemctl`, `launchctl`, `diskutil erase`, `mkfs`, `dd if=`
- `.env`, SSH 키, AWS/GCP/Kubernetes credential, `.npmrc`, `.pypirc` 등 민감 파일 읽기/쓰기

## 코드 변경 시

- Python 파일을 수정하면 가능한 경우 `ruff check`로 자체 점검한다 (post-edit hook이 자동 실행).
- 새 함수/모듈을 추가할 때는 기존 import 경로 컨벤션을 따른다 (예: 테스트가 `from utils import ...`를
  쓴다면 동일하게 유지).
- 변경 범위는 요청된 작업에 한정한다. 부수적인 "개선"은 별도로 제안만 한다.

## 라우팅 (이 프로젝트의 ai-agent 전용)

- 작업 범위가 작고 명확하면(1~3개 파일) 로컬 LLM이 처리한다.
- 대규모 리팩토링/아키텍처 변경/멀티파일 의존성 분석은 Claude Code 또는 Codex로 위임한다.
- 위임 시에도 위 guardrail과 금지 사항은 동일하게 적용된다.

---

# 프로젝트별 설정 (사용자 작성란)

> 위 내용은 ai-agent에 **공통(전역)으로 내장된 지침**입니다.
> 아래는 이 에이전트를 **개인/사내 프로젝트에 설치한 뒤, 그 프로젝트에 맞게 사용자가 직접 채우는 칸**입니다.
> (전역 지침은 ai-agent 저장소에서 관리되고, 프로젝트별 차이는 여기에만 적습니다.)
>
> **적용되려면 이 파일을 설치(프로젝트) 루트에 두세요 — `workspace/`(work_dir) 안이 아니라 그 바깥입니다.**
> agent가 실행 시 설치 루트(`root`)의 `AGENTS.md`(없으면 `CLAUDE.md`)를 읽어 파일선택·계획·라우팅
> 프롬프트에 자동 주입합니다. 즉 이 한 파일이 **전역(ai-agent) 설정 + 프로젝트별 추가 설정**을 함께 담습니다.
> workspace 안의 코드와 섞이지 않도록 지침은 항상 바깥(루트)에 둡니다.

## 프로젝트 개요

<!-- 이 프로젝트가 무엇인지 한두 줄로. 예: "결제 정산 배치 서비스 (Python/FastAPI)" -->

## 빌드 / 테스트 / 린트 명령

<!-- config.yaml의 verify_commands와 일치시킬 것. 예:
- 테스트: `pytest -q`
- 린트:   `ruff check .`
- 빌드:   `make build`
-->

## 이 프로젝트에서 추가로 금지/주의할 것

<!-- 위 공통 금지 사항 외에 이 프로젝트만의 제약. 예:
- `migrations/` 디렉토리는 수동 검토 없이 수정 금지
- 운영 DB 접속 정보가 든 `config/prod.yaml` 읽기/쓰기 금지
-->

## 프로젝트별 라우팅 / 컨벤션

<!-- 코드 스타일, import 컨벤션, 위임 기준 조정 등. 예:
- 모든 신규 모듈은 `src/` 하위에 배치
- 외부 API 호출 코드는 항상 Claude Code로 위임(보안 검토 필요)
-->

## 기타

<!-- 그 외 이 프로젝트에서 에이전트가 알아야 할 점 -->

