"""ai-agent 메인 진입점

사용법:
    python agent.py "작업 설명"
"""
import os
import sys
import time
import yaml

from backends.local_llm import LocalLLM
from backends.openrouter import OpenRouterLLM
from backends import claude_code_cli, codex_cli
from router import Router, is_chatter, reply_chatter, pick_external_tool, tool_enabled
from harness import context, planner, executor, verifier, recovery, hooks, metrics, project_guide


def _build_main_llm(cfg: dict):
    """파이프라인(파일선택/계획/라우팅/잡담판단)에 쓸 메인 LLM을 고른다.

    local_llm이 켜져 있으면 그걸 쓰고, 꺼져 있으면 openrouter를 시도한다.
    둘 다 꺼져 있으면 None(호출 측에서 외부 도구로 위임).
    """
    llm_cfg = cfg.get("local_llm", {})
    if llm_cfg.get("enabled", True):
        return LocalLLM(
            model=llm_cfg["model"],
            base_url=llm_cfg["base_url"],
            temperature=llm_cfg.get("temperature", 0.2),
        )
    or_cfg = cfg.get("openrouter", {})
    if or_cfg.get("enabled", False):
        return OpenRouterLLM(
            model=or_cfg.get("model", ""),
            api_key=or_cfg.get("api_key", ""),
            base_url=or_cfg.get("base_url", "https://openrouter.ai/api/v1"),
            temperature=or_cfg.get("temperature", 0.2),
        )
    return None


def _build_local_llm(cfg: dict):
    """local_llm이 켜져 있으면 Ollama LLM을 만든다."""
    llm_cfg = cfg.get("local_llm", {})
    if not llm_cfg.get("enabled", True):
        return None
    return LocalLLM(
        model=llm_cfg["model"],
        base_url=llm_cfg["base_url"],
        temperature=llm_cfg.get("temperature", 0.2),
    )


def _build_openrouter_llm(cfg: dict):
    """OpenRouter가 켜져 있으면 별도 위임용 LLM을 만든다."""
    or_cfg = cfg.get("openrouter", {})
    if not or_cfg.get("enabled", False):
        return None
    return OpenRouterLLM(
        model=or_cfg.get("model", ""),
        api_key=or_cfg.get("api_key", ""),
        base_url=or_cfg.get("base_url", "https://openrouter.ai/api/v1"),
        temperature=or_cfg.get("temperature", 0.2),
    )


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _default_confirm(question: str) -> bool:
    """TTY에서 y/n을 묻는다. 비대화형(stdin이 tty 아님)이면 적용하지 않는다(False)."""
    if not sys.stdin.isatty():
        return False
    try:
        ans = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _run_external(tool: str, task: str, run_root: str, apply: bool) -> dict:
    if tool == "codex":
        return codex_cli.run_codex(task, run_root, apply=apply)
    return claude_code_cli.run_claude_code(task, run_root, apply=apply)


def _delegate_external(tool: str, task: str, hook_roots, run_root: str, log_fn, confirm_fn=None) -> None:
    """외부 도구(claude_code / codex)로 작업을 위임한다.

    먼저 적용 없이 제안을 출력(미리보기)하고, 사용자가 허락하면 그때 파일에 적용한다.

    hook_roots:  가드레일 hook 검색 경로(프레임워크 루트 + 프로젝트 루트).
    run_root:    외부 도구가 실제로 작업할 프로젝트 폴더(work_root).
    confirm_fn:  적용 여부를 묻는 콜백(question -> bool). None이면 묻지 않고 제안만 출력.
    """
    try:
        hooks.check_pre_bash(hook_roots, f"{tool} {task}")
    except hooks.HookBlocked as e:
        log_fn(f"[차단됨] {e.message}")
        return

    # 1) 미리보기 — 파일을 고치지 않고 제안만 생성
    log_fn(f"[외부] {tool}로 변경 제안 생성 중...")
    preview = _run_external(tool, task, run_root, apply=False)
    log_fn(preview["output"])
    if not preview["success"]:
        return

    # 2) 허락 — OK면 적용, 아니면 제안만 출력하고 종료
    if confirm_fn is None or not confirm_fn(f"위 변경을 {tool}로 파일에 적용할까요?"):
        log_fn("[안내] 적용하지 않았습니다 (제안만 출력).")
        return

    # 3) 적용
    log_fn(f"[적용] {tool}로 변경을 파일에 반영합니다...")
    applied = _run_external(tool, task, run_root, apply=True)
    log_fn(applied["output"])


def _run_change_pipeline(llm, task: str, files: list[str], file_contents: dict[str, str],
                         work_root: str, hook_roots, cfg: dict, log_fn,
                         guide: str = "", step_prefix: str = "") -> dict:
    """주어진 LLM으로 계획→변경→검증 파이프라인을 실행한다.

    반환 dict의 success=False는 상위 호출자가 다음 백엔드로 폴백해도 된다는 뜻이다.
    """
    label = f"{step_prefix} " if step_prefix else ""
    log_fn(f"{label}[계획] 변경 계획 수립 중...")
    try:
        plan = planner.make_plan(llm, task, file_contents, guide=guide)
    except Exception as exc:
        log_fn(f"{label}[오류] 계획 생성 실패: {exc}")
        return {"success": False, "reason": "plan_failed", "error": str(exc)}

    if not isinstance(plan, dict):
        log_fn(f"{label}[오류] 계획 응답 형식이 올바르지 않습니다: {plan!r}")
        return {"success": False, "reason": "plan_failed", "error": "invalid_plan_type"}

    changes = plan.get("changes") or []
    if not isinstance(changes, list):
        log_fn(f"{label}[오류] 계획의 changes가 배열이 아닙니다: {changes!r}")
        return {"success": False, "reason": "plan_failed", "error": "invalid_changes_type"}

    if not changes:
        log_fn(f"{label}[안내] 수행할 변경 사항이 없습니다.")
        return {"success": False, "reason": "no_changes"}

    plan["changes"], dropped = planner.validate_changes(changes, files)
    for raw, why in dropped:
        log_fn(f"{label}[검증] 변경 항목 폐기: {raw!r} ({why})")

    if not plan["changes"]:
        return {"success": False, "reason": "invalid_changes"}

    missing = planner.untouched_targets(task, files, plan["changes"])
    if missing:
        log_fn(f"{label}[검증] 작업이 지정한 파일을 계획이 건드리지 않음: {missing}")
        return {"success": False, "reason": "offtarget", "missing": missing}

    for c in plan["changes"]:
        log_fn(f"  - {c['action']}: {c['file']} :: {c['description']}")

    backup_dir = cfg["harness"]["backup_dir"]
    max_retries = cfg["harness"]["max_recovery_retries"]
    timeout = cfg["harness"]["verify_timeout_sec"]

    verify_ran = False
    all_passed_final = True
    recovery_attempts_total = 0

    for change in plan["changes"]:
        f = change["file"]
        current = file_contents.get(f, "")
        log_fn(f"\n{label}[변경] 변경 생성 중: {f}")
        new_content, diff = executor.generate_change(llm, f, current, change["description"])
        log_fn(diff if diff.strip() else "(diff 없음 - 새 파일)")

        try:
            hooks.check_pre_file(hook_roots, f)
        except hooks.HookBlocked as e:
            log_fn(f"{label}[차단됨] {f} 변경을 건너뜁니다: {e.message}")
            all_passed_final = False
            continue

        try:
            backup_path = executor.apply_change(work_root, f, new_content, backup_dir)
        except executor.PathEscapeError:
            log_fn(f"{label}[차단] {f}: 작업 폴더(work_root) 밖 경로라 적용을 거부했습니다.")
            all_passed_final = False
            continue
        log_fn(f"{label}[변경] 적용 완료: {f} (백업: {backup_path})")

        post_edit_output = hooks.run_post_edit(hook_roots, f)
        if post_edit_output:
            log_fn(f"{label}[post-edit] {post_edit_output}")

        verify_cmds = plan.get("verify_commands", [])
        if not verify_cmds:
            log_fn(f"{label}[검증] 검증 명령 없음, 건너뜀")
            continue

        log_fn(f"{label}[검증] 실행 중...")
        blocked = False
        for cmd in verify_cmds:
            try:
                hooks.check_pre_bash(hook_roots, cmd)
            except hooks.HookBlocked as e:
                log_fn(f"{label}[차단됨] 검증 명령 차단: {e.message}")
                blocked = True
        if blocked:
            all_passed_final = False
            continue

        results = verifier.run_verification(verify_cmds, work_root, timeout)
        verify_ran = True
        for r in results:
            log_fn(f"  $ {r['cmd']} -> {'OK' if r['success'] else 'FAIL'}")
            if not r["success"]:
                log_fn(f"    --- output ---\n{r['output'][:2000]}\n    --------------")

        attempt = 0
        while not verifier.all_passed(results) and attempt < max_retries:
            attempt += 1
            failed = next(r for r in results if not r["success"])
            log_fn(f"{label}[복구] 검증 실패, 자동 복구 시도 {attempt}/{max_retries}")
            with open(f"{work_root}/{f}", "r", encoding="utf-8") as fp:
                cur = fp.read()
            recovery.recover_file(llm, work_root, f, cur, failed)
            results = verifier.run_verification(verify_cmds, work_root, timeout)
            for r in results:
                log_fn(f"  $ {r['cmd']} -> {'OK' if r['success'] else 'FAIL'}")
                if not r["success"]:
                    log_fn(f"    --- output ---\n{r['output'][:2000]}\n    --------------")

        recovery_attempts_total += attempt
        if verifier.all_passed(results):
            log_fn(f"{label}[완료] {f}: 검증 통과")
        else:
            all_passed_final = False
            log_fn(f"{label}[실패] {f}: {max_retries}회 시도 후에도 검증 실패. 백업: {backup_path}")

    return {
        "success": all_passed_final,
        "reason": "completed" if all_passed_final else "completed_with_failures",
        "verify_ran": verify_ran,
        "all_passed_final": all_passed_final,
        "recovery_attempts_total": recovery_attempts_total,
    }


def _delegate_openrouter(task: str, files: list[str], file_contents: dict[str, str],
                         work_root: str, hook_roots, cfg: dict, log_fn,
                         guide: str = "") -> dict:
    """OpenRouter가 켜져 있으면 외부 CLI 대신 먼저 코딩 파이프라인을 시도한다."""
    llm = _build_openrouter_llm(cfg)
    if llm is None:
        return {"attempted": False, "success": False, "reason": "disabled"}
    log_fn("[OpenRouter] 위임을 먼저 시도합니다.")
    if not llm.health_check():
        log_fn("[OpenRouter] 연결 실패 또는 API 키 없음. 외부 도구로 폴백합니다.")
        return {"attempted": True, "success": False, "reason": "health_check_failed"}
    try:
        result = _run_change_pipeline(
            llm, task, files, file_contents, work_root, hook_roots, cfg, log_fn,
            guide=guide, step_prefix="[OpenRouter]",
        )
    except Exception as exc:
        log_fn(f"[OpenRouter] 실패: {exc}. 외부 도구로 폴백합니다.")
        return {"attempted": True, "success": False, "reason": "exception"}
    result["attempted"] = True
    if result.get("success"):
        log_fn("[OpenRouter] 작업 완료")
    else:
        log_fn(f"[OpenRouter] 완료하지 못함({result.get('reason')}). 외부 도구로 폴백합니다.")
    return result


EXPLAIN_PROMPT = """다음은 사용자의 질문과 관련 코드다.

질문: {task}

관련 파일:
{file_contents}

코드를 바탕으로 한국어로 명확하게 설명해라. 코드를 수정하지 말고 설명만 해라."""

_EXPLAIN_MAX_FILE_CHARS = 12000
_EXPLAIN_MAX_TOTAL_CHARS = 36000

# 설명/질문 의도를 나타내는 키워드 (수정 키워드가 함께 있으면 수정으로 본다)
_EXPLAIN_HINTS = ("설명", "분석", "무슨", "뭐하는", "뭐야", "뭐니", "어떻게 동작", "동작 방식",
                  "왜", "알려줘", "요약", "explain", "what is", "what does", "describe", "summar")
_EDIT_HINTS = ("추가", "수정", "고쳐", "고쳐줘", "바꿔", "변경", "구현", "리팩토", "삭제", "제거",
               "생성", "만들어", "작성", "fix", "add", "implement", "refactor", "create", "remove")

_CODE_EXTS = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".rs",
              ".c", ".cc", ".cpp", ".h", ".rb", ".php", ".kt", ".swift",
              ".cs", ".csproj", ".sln", ".md")
_SUMMARY_HINTS = ("프로젝트", "폴더", "간략", "요약", "뭐하는", "무슨", "summary", "overview")
_SUMMARY_PRIORITY = ("readme.md", ".sln", ".csproj", "package.json", "pyproject.toml",
                     "pom.xml", "build.gradle", "settings.gradle")


def _is_general_information_request(task: str) -> bool:
    """자기소개·백엔드·도구 개념 질문처럼 프로젝트 코드가 아닌 일반 지식/설명 요청인지 판별한다."""
    if not task:
        return False
    t = task.lower()
    if "내 프로젝트" in t or "프로젝트 설명" in t or "프로젝트 1문장" in t:
        return False
    patterns = (
        "너에대해", "너는", "너의", "뭐야", "뭐니", "무슨", "어떤", "어떻게", "설명해줘",
        "설명해", "알려줘", "알려주", "openrouter", "ollama", "claude", "codex",
        "agent", "ai agent", "에이전트", "백엔드", "모델", "api key"
    )
    return any(p in t for p in patterns)


def _looks_like_question(task: str) -> bool:
    """설명/질문형 작업이면 True. 수정 키워드가 있으면 수정 작업으로 간주한다."""
    if not task:
        return False
    t = task.lower()
    if any(k in task or k in t for k in _EDIT_HINTS):
        return False
    if any(k in task or k in t for k in _EXPLAIN_HINTS):
        return True
    return bool(t.endswith("?")) or bool(t.endswith("요?")) or bool(t.endswith("니?")) or bool(t.endswith("야?"))


def _fallback_read_files(task: str, tree_paths: list) -> list:
    """파일 선택이 비었을 때의 대체 선택.

    작업에 명시된 파일/폴더명 → 프로젝트 요약용 메타 파일 → 소스 코드 파일 순.
    """
    task_l = (task or "").lower()
    mentioned = [
        p for p in tree_paths
        if os.path.basename(p).lower() in task_l
        or p.lower() in task_l
        or any(part and part.lower() in task_l for part in p.split(os.sep)[:-1])
    ]
    if mentioned:
        return _rank_summary_files(mentioned)[:5]
    if any(h in task_l for h in _SUMMARY_HINTS):
        summary = _rank_summary_files(tree_paths)
        if summary:
            return summary[:5]
    code = [p for p in tree_paths if p.endswith(_CODE_EXTS)]
    return code[:5]


def _rank_summary_files(paths: list[str]) -> list[str]:
    """프로젝트 설명에 도움이 되는 파일을 앞으로 정렬한다."""
    def score(path: str) -> tuple[int, str]:
        lower = path.lower()
        base = os.path.basename(lower)
        for idx, pat in enumerate(_SUMMARY_PRIORITY):
            if base == pat or lower.endswith(pat):
                return (idx, lower)
        if lower.endswith(".md"):
            return (20, lower)
        if lower.endswith((".csproj", ".sln")):
            return (30, lower)
        if lower.endswith(".cs"):
            return (40, lower)
        if lower.endswith(_CODE_EXTS):
            return (50, lower)
        return (100, lower)

    ranked = [p for p in paths if score(p)[0] < 100]
    return sorted(ranked, key=score)


def _format_explain_contents(file_contents: dict[str, str]) -> str:
    """설명 모드 프롬프트가 너무 커지지 않게 파일별/전체 길이를 제한한다."""
    parts = []
    used = 0
    for f, content in file_contents.items():
        if not content:
            continue
        remaining = _EXPLAIN_MAX_TOTAL_CHARS - used
        if remaining <= 0:
            break
        limit = min(_EXPLAIN_MAX_FILE_CHARS, remaining)
        clipped = content[:limit]
        if len(content) > limit:
            clipped += f"\n\n... (이하 {len(content) - limit}자 생략)"
        block = f"### {f}\n```\n{clipped}\n```"
        parts.append(block)
        used += len(clipped)
    return "\n\n".join(parts) or "(파일 내용을 읽지 못했습니다)"


def explain_task(llm, task: str, work_root: str, exclude_dirs, log_fn, guide: str = "") -> None:
    """수정 없이 코드를 읽고 질문에 답한다 (설명 모드)."""
    log_fn("[설명 모드] 코드를 읽고 답합니다 (수정하지 않음)")
    tree_paths = [p for p in context.get_project_tree(work_root, exclude_dirs).split("\n") if p]
    files = context.select_relevant_files(llm, task, work_root, exclude_dirs, guide=guide)
    if not files:
        files = _fallback_read_files(task, tree_paths)
    if not files:
        log_fn("[안내] 읽을 코드 파일을 찾지 못했습니다. workspace/ 에 코드가 있는지 확인하세요.")
        return

    log_fn(f"[설명 모드] 읽는 파일: {files}")
    file_contents = context.read_files(work_root, files)
    contents_str = _format_explain_contents(file_contents)
    try:
        answer = llm.generate(EXPLAIN_PROMPT.format(task=task, file_contents=contents_str))
    except Exception as exc:
        log_fn(f"[오류] 설명 생성 실패: {exc}")
        log_fn("        OpenRouter 모델명/API 키/컨텍스트 제한을 확인하세요.")
        return
    log_fn("")
    if isinstance(answer, str):
        if answer.strip():
            log_fn(answer)
        else:
            log_fn("[안내] 모델이 빈 답변을 반환했습니다. 잠시 후 다시 시도하거나 /model claude 로 위임해 보세요.")
    else:
        log_fn(str(answer) if answer is not None else "[안내] 모델이 빈 답변을 반환했습니다. 잠시 후 다시 시도하거나 /model claude 로 위임해 보세요.")


def run_agent(task: str, root: str = ".", log_fn=None, force: str = None, confirm_fn=None):
    """작업을 실행한다.

    force: None/"auto" → 자동 라우팅, "local" → 로컬 LLM 강제,
           "openrouter" → OpenRouter 강제,
           "claude" → Claude Code 강제, "codex" → Codex 강제.
    confirm_fn: 외부 도구 변경을 파일에 적용할지 묻는 콜백. None이면 TTY 입력(_default_confirm).
    """
    if log_fn is None:
        log_fn = print
    if confirm_fn is None:
        confirm_fn = _default_confirm

    t0 = time.monotonic()
    cfg = load_config()
    log_dir = cfg.get("logging", {}).get("log_dir", "logs")

    def _rec(decision, outcome, **extra):
        """작업 1건 지표 기록 (실패해도 무시)."""
        metrics.record_run(log_dir, {
            "task": task[:200],
            "decision": decision,
            "outcome": outcome,
            "duration_sec": round(time.monotonic() - t0, 2),
            **extra,
        })

    # 프레임워크(root)와 수정 대상 프로젝트(work_root)를 분리한다.
    # config의 harness.work_dir 하위 폴더가 실제 작업 대상. 가드레일 hook은 root에 유지.
    work_dir = cfg.get("harness", {}).get("work_dir", ".")
    work_root = os.path.normpath(os.path.join(root, work_dir))
    os.makedirs(work_root, exist_ok=True)
    if work_dir not in (".", ""):
        log_fn(f"[작업 폴더] {work_root}")

    # hook 검색 경로: 프레임워크 루트(공통 가드레일) + 프로젝트 루트(프로젝트별 hook)
    hook_roots = [root, work_root]

    # 지침(AGENTS.md)을 로컬 프롬프트(파일선택/계획/라우팅)에 주입한다.
    # workspace(work_root) 안이 아니라 **설치/프로젝트 루트**의 AGENTS.md를 본다.
    # = 전역 ai-agent 설정 + 프로젝트별 추가 설정이 함께 담긴 곳.
    guide = project_guide.load([root])
    if guide:
        log_fn("[프로젝트 지침] AGENTS.md 적용")

    # 사용자가 외부 도구를 강제 지정한 경우: Ollama 없이 바로 위임한다.
    if force in ("claude", "codex"):
        tool = "codex" if force == "codex" else "claude_code"
        if not tool_enabled(cfg, tool):
            log_fn(f"[오류] {tool}가 config.yaml에서 비활성화되어 있습니다 (external_tools.{tool}.enabled: false).")
            _rec("n/a", "tool_disabled", tool=tool)
            return
        log_fn(f"[1/2] 작업: {task}")
        log_fn(f"[2/2] 외부 도구로 위임 (사용자 지정): {tool}")
        _delegate_external(tool, task, hook_roots, work_root, log_fn, confirm_fn)
        _rec("external", "forced_external", tool=tool)
        return

    if force == "local":
        main_llm = _build_local_llm(cfg)
    elif force == "openrouter":
        main_llm = _build_openrouter_llm(cfg)
    else:
        main_llm = _build_main_llm(cfg)

    if force == "local" and main_llm is None:
        log_fn("[오류] local_llm이 config.yaml에서 비활성화되어 있습니다.")
        _rec("n/a", "local_disabled")
        return
    if force == "openrouter" and main_llm is None:
        log_fn("[오류] openrouter가 config.yaml에서 비활성화되어 있습니다 (openrouter.enabled: false).")
        _rec("n/a", "openrouter_disabled")
        return

    # 파이프라인 백엔드(local_llm/openrouter)가 모두 비활성화된 경우: 바로 외부로 위임한다.
    if main_llm is None:
        tool = pick_external_tool(cfg)
        if tool is None:
            log_fn("[오류] local_llm/openrouter/외부 도구가 모두 비활성화되어 있습니다. "
                   "config.yaml에서 최소 하나는 enabled: true로 설정하세요.")
            _rec("n/a", "no_enabled_backend")
            return
        log_fn(f"[1/2] 작업: {task}")
        log_fn(f"[2/2] 파이프라인 백엔드 비활성화 → 외부 도구로 위임: {tool}")
        _delegate_external(tool, task, hook_roots, work_root, log_fn, confirm_fn)
        _rec("external", "local_disabled_external", tool=tool)
        return

    if not main_llm.health_check():
        if isinstance(main_llm, OpenRouterLLM):
            tool = pick_external_tool(cfg)
            if tool is None:
                log_fn("[오류] OpenRouter에 연결할 수 없고 활성화된 외부 도구도 없습니다. "
                       "config.yaml의 openrouter.api_key 또는 external_tools 설정을 확인하세요.")
                _rec("n/a", "openrouter_unavailable_no_tool")
                return
            log_fn(f"[OpenRouter] 연결 실패 또는 API 키 없음 → 외부 도구로 위임: {tool}")
            _delegate_external(tool, task, hook_roots, work_root, log_fn, confirm_fn)
            _rec("external", "openrouter_unavailable_external", tool=tool)
        else:
            log_fn("[오류] Ollama 서버에 연결할 수 없습니다. 'ollama serve'가 실행 중인지 확인하세요.")
        return

    exclude_dirs = cfg["harness"]["exclude_dirs"]

    # 설명/질문형 작업은 수정 파이프라인 대신 읽기 전용 설명 모드로 처리한다.
    if force != "claude" and force != "codex" and _looks_like_question(task):
        if _is_general_information_request(task):
            try:
                answer = main_llm.generate(
                    f"사용자 질문: {task}\n\n당신은 AI 에이전트의 설명자다. 짧고 명확하게 한국어로 답해라. 프로젝트 파일을 읽지 말고, 일반적인 의미로 설명해라.",
                    num_predict=200,
                )
            except Exception as exc:
                log_fn(f"[오류] 설명 생성 실패: {exc}")
                log_fn("        OpenRouter 모델명/API 키/컨텍스트 제한을 확인하세요.")
                return
            if isinstance(answer, str) and answer.strip():
                log_fn(answer)
            else:
                log_fn("[안내] 모델이 빈 답변을 반환했습니다. 잠시 후 다시 시도해 주세요.")
            _rec("openrouter" if force == "openrouter" else "local", "explain")
            return
        explain_task(main_llm, task, work_root, exclude_dirs, log_fn, guide=guide)
        _rec("openrouter" if force == "openrouter" else "local", "explain")
        return

    # 코딩 작업이 아닌 일상대화/잡담은 파이프라인 진입 전에 즉시 걸러낸다.
    if force not in ("claude", "codex", "local") and is_chatter(main_llm, task):
        log_fn(reply_chatter(main_llm, task))
        _rec("n/a", "chatter")
        return

    log_fn(f"[1/6] 작업: {task}")

    # Step 1: 관련 파일 탐색 (work_root 기준)
    files = context.select_relevant_files(main_llm, task, work_root, exclude_dirs, guide=guide)
    log_fn(f"[1/6] 선택된 파일: {files}")

    if not files:
        log_fn("[안내] 작업과 관련된 파일을 찾지 못했습니다.")
        log_fn("        설명/질문이 목적이면 질문 형태로 다시 입력하거나, /model claude 로 위임하세요.")
        _rec("n/a", "no_files")
        return

    file_contents = context.read_files(work_root, files)
    est_tokens = context.estimate_tokens(file_contents)

    # Step 2: 라우팅 판단 (force == "local/openrouter"이면 라우팅을 건너뛰고 해당 LLM 강제)
    if force in ("local", "openrouter"):
        decision = {"decision": "local", "reason": f"사용자 지정(/model {force})", "tool": None}
    else:
        router = Router(main_llm, cfg, guide=guide)
        decision = router.decide(task, len(files), est_tokens)
    log_fn(f"[2/6] 라우팅 결정: {decision}")

    if decision["decision"] == "external":
        or_result = _delegate_openrouter(task, files, file_contents, work_root, hook_roots, cfg, log_fn, guide=guide)
        if or_result.get("success"):
            _rec("openrouter", "auto_openrouter", est_tokens=est_tokens, files=len(files),
                 verify_ran=or_result.get("verify_ran", False),
                 all_passed_final=or_result.get("all_passed_final", True),
                 recovery_attempts_total=or_result.get("recovery_attempts_total", 0))
            log_fn("\n[작업 종료]")
            return

        tool = decision.get("tool") or pick_external_tool(cfg)
        if tool is None:
            if or_result.get("attempted"):
                log_fn("[오류] OpenRouter 위임이 실패했고 활성화된 외부 도구도 없습니다.")
            else:
                log_fn("[오류] 활성화된 외부 도구가 없습니다. config.yaml의 external_tools에서 하나를 enabled: true로 설정하세요.")
            _rec("n/a", "no_enabled_tool", est_tokens=est_tokens, files=len(files),
                 openrouter_attempted=or_result.get("attempted", False))
            return
        if or_result.get("attempted"):
            log_fn(f"[2/6] OpenRouter 실패 후 외부 도구로 위임: {tool}")
        else:
            log_fn(f"[2/6] OpenRouter 비활성화 → 외부 도구로 위임: {tool}")
        _delegate_external(tool, task, hook_roots, work_root, log_fn, confirm_fn)
        _rec("external", "auto_external", tool=tool, est_tokens=est_tokens, files=len(files),
             openrouter_attempted=or_result.get("attempted", False))
        return

    local_result = _run_change_pipeline(
        main_llm, task, files, file_contents, work_root, hook_roots, cfg, log_fn, guide=guide,
    )
    if not local_result.get("success"):
        if force == "openrouter":
            tool = pick_external_tool(cfg)
            if tool is None:
                log_fn("[안내] OpenRouter가 작업을 완료하지 못했고, 활성화된 외부 도구도 없습니다.")
                _rec("n/a", "openrouter_fallback_no_tool", est_tokens=est_tokens, files=len(files),
                     openrouter_reason=local_result.get("reason"))
                return
            log_fn(f"[폴백] OpenRouter 실패 후 외부 도구로 위임: {tool}")
            _delegate_external(tool, task, hook_roots, work_root, log_fn, confirm_fn)
            _rec("external", "openrouter_fallback_external", tool=tool, est_tokens=est_tokens, files=len(files),
                 openrouter_reason=local_result.get("reason"))
            return

        or_result = _delegate_openrouter(task, files, file_contents, work_root, hook_roots, cfg, log_fn, guide=guide)
        if or_result.get("success"):
            _rec("openrouter", "local_fallback_openrouter", est_tokens=est_tokens, files=len(files),
                 local_reason=local_result.get("reason"),
                 verify_ran=or_result.get("verify_ran", False),
                 all_passed_final=or_result.get("all_passed_final", True),
                 recovery_attempts_total=or_result.get("recovery_attempts_total", 0))
            log_fn("\n[작업 종료]")
            return

        tool = pick_external_tool(cfg)
        if tool is None:
            log_fn("[안내] 로컬/OpenRouter가 작업을 완료하지 못했고, 활성화된 외부 도구도 없습니다.")
            _rec("n/a", "fallback_no_tool", est_tokens=est_tokens, files=len(files),
                 local_reason=local_result.get("reason"),
                 openrouter_attempted=or_result.get("attempted", False))
            return
        if or_result.get("attempted"):
            log_fn(f"[폴백] OpenRouter 실패 후 외부 도구로 위임: {tool}")
        else:
            log_fn(f"[폴백] OpenRouter 비활성화 → 외부 도구로 위임: {tool}")
        _delegate_external(tool, task, hook_roots, work_root, log_fn, confirm_fn)
        _rec("external", "local_fallback_external", tool=tool, est_tokens=est_tokens, files=len(files),
             local_reason=local_result.get("reason"),
             openrouter_attempted=or_result.get("attempted", False))
        return

    _rec("openrouter" if force == "openrouter" else "local", "completed", est_tokens=est_tokens, files=len(files),
         verify_ran=local_result.get("verify_ran", False),
         all_passed_final=local_result.get("all_passed_final", True),
         recovery_attempts_total=local_result.get("recovery_attempts_total", 0))
    log_fn("\n[작업 종료]")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('사용법: python agent.py "작업 설명"')
        sys.exit(1)
    run_agent(" ".join(sys.argv[1:]))
