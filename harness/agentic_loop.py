"""도구 호출 기반 반복 루프(에이전틱 루프).

모델이 매 턴마다 "다음에 뭘 할지"를 도구 호출 하나로 스스로 고르고, 그
결과를 다시 보여준 뒤 또 고르게 하는 것을 모델이 done을 선언하거나
최대 스텝 수에 닿을 때까지 반복한다. local/external 구분 없이 모든
작업이 이 루프 하나로 처리된다.

권한 모드(mode)에 따라 write_file/run_command 실행 전 사용자 승인이
붙는다:
- manual:    write_file, run_command 둘 다 매번 승인
- edit-only: write_file은 자동 적용, run_command만 승인 (기본값)
- auto:      둘 다 자동 실행 (pre-bash/pre-file hook 차단은 항상 유효)
"""
import difflib
import json
import os
import re
from datetime import datetime

from harness import context, hooks, verifier
from harness.executor import (
    BlockApplyError, PathEscapeError, apply_blocks, apply_change, safe_full_path,
)

TRACE_LOG_FILE = "trace.jsonl"

MAX_STEPS_DEFAULT = 20
_MAX_TOOL_OUTPUT_CHARS = 4000
_MAX_READ_CHARS = 12000
_HISTORY_KEEP = 30
MODES = ("manual", "edit-only", "auto")
DEFAULT_MODE = "edit-only"

# 동일/유사한 도구 호출이 이만큼 연속으로 반복되면(진행 없이 토큰만 소모하는
# 루프로 간주) done/max_steps를 기다리지 않고 즉시 중단한다.
_REPEAT_LOOP_THRESHOLD = 3
# 작업 1건 비용 상한 기본값(달러). config.yaml의 harness.max_cost_usd로 조정 가능.
# None/0이면 비활성화.
_MAX_COST_USD_DEFAULT = None

SYSTEM_PROMPT = """당신은 코딩 에이전트다. 아래 도구를 매 턴 하나씩 호출해서
작업을 완료해라. 파일 내용을 추측하지 말고, 필요하면 반드시 read_file이나
grep으로 먼저 확인해라.
{conversation}
작업: {task}
작업 루트: {work_root} (모든 경로는 이 루트 기준 상대경로)
{guide}
사용 가능한 도구:

1. list_dir - 디렉토리의 파일/폴더 목록을 본다.
   {{"tool": "list_dir", "path": "."}}

2. grep - 파일 내용에서 패턴을 검색한다 (파일명이 아니라 내용을 찾을 때).
   {{"tool": "grep", "pattern": "검색어"}}

3. read_file - 파일 내용을 읽는다.
   {{"tool": "read_file", "path": "src/foo.py"}}

4. write_file - 파일을 수정하거나 새로 만든다.
   기존 파일: SEARCH/REPLACE 블록으로 바뀔 부분만 지정한다. SEARCH는 read_file로
   확인한 실제 내용과 공백 하나까지 정확히 일치해야 한다.
   새 파일: content에 전체 내용을 담는다.
   {{"tool": "write_file", "path": "src/foo.py", "search": "old code", "replace": "new code"}}
   {{"tool": "write_file", "path": "src/new.py", "content": "전체 파일 내용"}}

5. run_command - 셸 명령을 실행한다 (빌드/테스트/린트 등 검증용).
   {{"tool": "run_command", "command": "pytest tests/ -q"}}

6. done - 작업이 끝났다고 판단되면 호출한다. 무엇을 했는지 요약해라.
   {{"tool": "done", "summary": "무엇을 했는지 한국어 요약"}}

지금까지의 진행 상황(도구 호출과 결과 이력)을 보고, 다음에 실행할 도구
하나를 JSON으로만 답해라. 다른 텍스트는 절대 쓰지 마라.
"""

TURN_PROMPT = """지금까지 진행 이력:
{history}

다음에 실행할 도구 하나를 JSON으로만 답해라."""


def _fmt_step(i: int, action: dict, result: str) -> str:
    return f"--- 스텝 {i} ---\n호출: {json.dumps(action, ensure_ascii=False)}\n결과:\n{result}\n"


def _action_signature(action: dict) -> str:
    """도구 호출을 반복 감지용으로 비교할 수 있는 문자열로 정규화한다.

    write_file은 매번 다른 content/search/replace를 담고 있어도 같은 파일에
    대한 반복 시도는 "진행 없음"의 신호이므로 path만 본다. 나머지 도구는
    입력 전체(JSON)가 같아야 진짜 반복으로 본다.
    """
    tool = action.get("tool")
    if tool == "write_file":
        return f"write_file:{action.get('path')}"
    return json.dumps(action, ensure_ascii=False, sort_keys=True)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit}자 생략)"


def _make_trace_writer(cfg: dict):
    """harness.trace_log가 켜져 있으면, 매 스텝(도구 호출/결과)을 마스킹 후
    logs/trace.jsonl에 append하는 함수를 반환한다. 꺼져 있으면 아무것도 하지 않는
    no-op 함수를 반환해 호출부가 매번 옵션 여부를 분기하지 않아도 되게 한다.

    감사(audit)/디버깅 목적의 전체 트레이스. 민감정보는 harness/metrics.py의
    redact_sensitive()로 마스킹한 뒤 기록한다 — fail-safe로 기록 자체가 실패해도
    에이전트 실행에는 영향을 주지 않는다.
    """
    harness_cfg = cfg.get("harness", {})
    if not harness_cfg.get("trace_log", False):
        return lambda step, action, result: None

    from harness import metrics as _metrics
    log_dir = cfg.get("logging", {}).get("log_dir", "logs")
    path = os.path.join(log_dir, TRACE_LOG_FILE)

    def _write(step: int, action: dict, result: str):
        try:
            os.makedirs(log_dir, exist_ok=True)
            row = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "step": step,
                "action": {k: _metrics.redact_sensitive(str(v)) if isinstance(v, str) else v
                           for k, v in action.items()},
                "result": _metrics.redact_sensitive(_truncate(result, _MAX_TOOL_OUTPUT_CHARS)),
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass  # trace log는 부가 기능 — 기록 실패가 작업 실행을 막으면 안 된다.

    return _write


def _dispatch(action: dict, work_root: str, hook_roots, cfg: dict, log_fn,
              mode: str = DEFAULT_MODE, confirm_fn=None) -> tuple[str, bool]:
    """도구 하나를 실행하고 (결과 텍스트, 종료여부)를 반환한다."""
    tool = action.get("tool")

    if tool == "list_dir":
        path = action.get("path", ".")
        try:
            full = safe_full_path(work_root, path)
        except PathEscapeError:
            return f"오류: {path}는 작업 루트 밖 경로라 접근을 거부했습니다.", False
        if not os.path.isdir(full):
            return f"오류: {path}는 디렉토리가 아니거나 존재하지 않습니다.", False
        entries = sorted(os.listdir(full))
        return "\n".join(entries) or "(빈 디렉토리)", False

    if tool == "grep":
        pattern = str(action.get("pattern", ""))
        if not pattern:
            return "오류: pattern이 비어있습니다.", False
        exclude_dirs = cfg.get("harness", {}).get("exclude_dirs", [])
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"오류: 잘못된 정규식입니다: {e}", False
        hits = []
        for dirpath, dirnames, filenames in os.walk(work_root):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs and not d.startswith(".")]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    with open(full, "r", encoding="utf-8") as fp:
                        for lineno, line in enumerate(fp, 1):
                            if rx.search(line):
                                rel = os.path.relpath(full, work_root)
                                hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                                if len(hits) >= 50:
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
                if len(hits) >= 50:
                    break
            if len(hits) >= 50:
                break
        return "\n".join(hits) if hits else "(매칭 없음)", False

    if tool == "read_file":
        path = action.get("path", "")
        try:
            full = safe_full_path(work_root, path)
        except PathEscapeError:
            return f"오류: {path}는 작업 루트 밖 경로라 접근을 거부했습니다.", False
        try:
            with open(full, "r", encoding="utf-8") as fp:
                content = fp.read()
        except FileNotFoundError:
            return f"오류: {path} 파일이 없습니다. (새 파일을 만들려면 write_file의 content를 써라)", False
        except UnicodeDecodeError:
            return f"오류: {path}는 텍스트로 읽을 수 없는 파일입니다.", False
        return _truncate(content, _MAX_READ_CHARS), False

    if tool == "write_file":
        path = action.get("path", "")
        if not path:
            return "오류: path가 비어있습니다.", False
        try:
            hooks.check_pre_file(hook_roots, path)
        except hooks.HookBlocked as e:
            return f"차단됨: {e.message}", False

        full = None
        try:
            full = safe_full_path(work_root, path)
        except PathEscapeError:
            return f"오류: {path}는 작업 루트 밖 경로라 적용을 거부했습니다.", False

        current = ""
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8") as fp:
                    current = fp.read()
            except UnicodeDecodeError:
                return f"오류: {path}는 텍스트로 읽을 수 없어 수정할 수 없습니다.", False

        if "content" in action:
            new_content = str(action["content"])
        elif "search" in action or "replace" in action:
            search = str(action.get("search", ""))
            replace = str(action.get("replace", ""))
            if not current:
                new_content = replace
            else:
                raw = f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"
                try:
                    new_content = apply_blocks(current, raw)
                except BlockApplyError as e:
                    return f"오류: {e} (read_file로 정확한 현재 내용을 다시 확인해라)", False
        else:
            return "오류: write_file은 content 또는 search/replace가 필요합니다.", False

        diff = "".join(difflib.unified_diff(
            current.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}",
        ))

        if mode == "manual":
            log_fn(diff if diff.strip() else f"(새 파일: {path})")
            if confirm_fn is not None and not confirm_fn(f"{path}에 위 변경을 적용할까요?"):
                return f"사용자가 {path} 변경 적용을 거부했습니다.", False

        backup_dir = cfg.get("harness", {}).get("backup_dir", ".agent_backup")
        backup_path = apply_change(work_root, path, new_content, backup_dir)
        log_fn(f"  [적용] {path} (백업: {backup_path})")
        post_edit_output = hooks.run_post_edit(hook_roots, path)
        result = f"{path}에 적용 완료."
        if post_edit_output:
            result += f"\npost-edit: {post_edit_output}"
        return result, False

    if tool == "run_command":
        command = action.get("command", "")
        if not command:
            return "오류: command가 비어있습니다.", False
        try:
            hooks.check_pre_bash(hook_roots, command)
        except hooks.HookBlocked as e:
            return f"차단됨: {e.message}", False
        if mode in ("manual", "edit-only"):
            if confirm_fn is not None and not confirm_fn(f"다음 명령을 실행할까요?\n$ {command}"):
                return f"사용자가 명령 실행을 거부했습니다: {command}", False
        timeout = cfg.get("harness", {}).get("verify_timeout_sec", 120)
        sandbox_env = cfg.get("harness", {}).get("sandbox_env", False)
        results = verifier.run_verification([command], work_root, timeout, sandbox_env=sandbox_env)
        r = results[0]
        status = "성공(exit 0)" if r["success"] else f"실패(exit {r['returncode']})"
        return f"{status}\n{_truncate(r['output'], _MAX_TOOL_OUTPUT_CHARS)}", False

    if tool == "done":
        summary = action.get("summary", "(요약 없음)")
        log_fn(f"  [완료 선언] {summary}")
        return summary, True

    return f"오류: 알 수 없는 도구입니다: {tool!r}. list_dir/grep/read_file/write_file/run_command/done 중 하나를 써라.", False


def run_loop(llm, task: str, work_root: str, hook_roots, cfg: dict, log_fn,
             guide: str = "", max_steps: int | None = None,
             mode: str = DEFAULT_MODE, confirm_fn=None,
             conversation_history: list[str] | None = None) -> dict:
    """에이전틱 루프를 실행한다.

    모델이 매 스텝 도구 호출 JSON을 하나 반환하면 실행하고 결과를 이력에
    쌓아 다음 프롬프트에 넘긴다. 종료 조건은 다음 중 하나:
      - done 호출
      - max_steps 도달
      - 동일/유사 도구 호출이 연속으로 반복되는 루프 감지(repeat_loop_detected)
      - 누적 비용이 harness.max_cost_usd를 초과(cost_limit_exceeded, 설정된 경우만)

    mode: "manual"(전부 승인) | "edit-only"(파일수정 자동, 명령만 승인)
          | "auto"(전부 자동). confirm_fn: 승인 질문(question:str) -> bool.
    conversation_history: 같은 대화형 세션의 직전 턴 입력들(최신순 아님, 오래된 것부터).
          "이번에 추가한 기능"처럼 이전 턴을 참조하는 후속 질문을 모델이 이해하도록
          시스템 프롬프트에 짧게 얹는다. None/빈 리스트면 생략.
    """
    harness_cfg = cfg.get("harness", {})
    max_steps = max_steps or harness_cfg.get("agentic_max_steps", MAX_STEPS_DEFAULT)
    max_cost_usd = harness_cfg.get("max_cost_usd", _MAX_COST_USD_DEFAULT)
    price_per_mtok = harness_cfg.get("price_per_mtok")
    if price_per_mtok is None:
        from harness import metrics as _metrics
        price_per_mtok = _metrics.DEFAULT_PRICE_PER_MTOK

    conv_text = context.format_conversation_history(conversation_history)
    system = SYSTEM_PROMPT.format(
        task=task, work_root=work_root, conversation=conv_text,
        guide=f"\n프로젝트 지침:\n{guide}\n" if guide else "",
    )

    history_entries = []
    steps_taken = 0
    files_touched = []
    done_summary = None
    stop_reason = None

    last_signature = None
    repeat_count = 0

    trace = _make_trace_writer(cfg)

    def _cumulative_cost_usd() -> float:
        """지금까지 이 llm 인스턴스가 소모한 누적 비용(추정, 달러).

        OpenRouterLLM은 cache_stats()로 캐시 통계까지 포함한 실제 usage를
        누적 제공한다(캐시 읽기는 ~10% 단가). 그 외(LocalLLM 등)는 비용
        개념이 없으므로 0으로 취급해 상한 검사를 건너뛴다.
        """
        cache_stats = getattr(llm, "cache_stats", None)
        if cache_stats is None:
            return 0.0
        stats = cache_stats()
        full_price_tokens = (
            stats.get("prompt_tokens", 0) + stats.get("completion_tokens", 0)
            - stats.get("cache_read_tokens", 0)
        )
        cost = full_price_tokens / 1_000_000 * price_per_mtok
        cost += stats.get("cache_read_tokens", 0) / 1_000_000 * price_per_mtok * 0.1
        return max(cost, 0.0)

    for step in range(1, max_steps + 1):
        history_text = "".join(history_entries) or "(아직 없음)"
        prompt = TURN_PROMPT.format(history=history_text)
        try:
            action = llm.generate(prompt, system=system, json_mode=True, num_predict=1024)
        except Exception as exc:
            log_fn(f"[에이전틱] 스텝 {step}: 모델 호출 실패 - {exc}")
            error_msg = f"오류: 모델 호출 실패 - {exc}"
            history_entries.append(_fmt_step(step, {"tool": "?"}, error_msg))
            trace(step, {"tool": "?"}, error_msg)
            steps_taken = step
            last_signature = None
            repeat_count = 0
        else:
            if not isinstance(action, dict) or "tool" not in action:
                log_fn(f"[에이전틱] 스텝 {step}: 잘못된 응답 형식 - {action!r}")
                error_msg = "오류: 도구 호출 JSON 형식이 아닙니다. {\"tool\": ...} 형식으로 답해라."
                history_entries.append(_fmt_step(step, {"tool": "?"}, error_msg))
                trace(step, {"tool": "?", "raw": repr(action)}, error_msg)
                steps_taken = step
                last_signature = None
                repeat_count = 0
            else:
                log_fn(f"[에이전틱] 스텝 {step}: {action.get('tool')} {action.get('path') or action.get('pattern') or action.get('command') or ''}")
                result, finished = _dispatch(action, work_root, hook_roots, cfg, log_fn, mode=mode, confirm_fn=confirm_fn)
                steps_taken = step
                trace(step, action, result)

                if action.get("tool") == "write_file" and "오류" not in result and "차단" not in result and "거부" not in result:
                    path = action.get("path")
                    if path and path not in files_touched:
                        files_touched.append(path)

                history_entries.append(_fmt_step(step, action, _truncate(result, _MAX_TOOL_OUTPUT_CHARS)))
                # 이력이 너무 길어지지 않게 최근 N개만 유지 (오래된 스텝은 요약 없이 버림)
                if len(history_entries) > _HISTORY_KEEP:
                    history_entries = history_entries[-_HISTORY_KEEP:]

                if finished:
                    done_summary = result
                    break

                # 반복 루프 감지: 같은 도구 호출(또는 같은 파일에 대한 write_file)이
                # 연속으로 이어지면 진행이 없다는 뜻이므로 토큰을 더 태우기 전에 멈춘다.
                sig = _action_signature(action)
                if sig == last_signature:
                    repeat_count += 1
                else:
                    last_signature = sig
                    repeat_count = 1
                if repeat_count >= _REPEAT_LOOP_THRESHOLD:
                    log_fn(f"[에이전틱] 동일한 도구 호출이 {repeat_count}회 연속 반복되어 중단합니다: {sig[:200]}")
                    stop_reason = "repeat_loop_detected"
                    break

        if max_cost_usd:
            cost_so_far = _cumulative_cost_usd()
            if cost_so_far >= max_cost_usd:
                log_fn(f"[에이전틱] 누적 비용(${cost_so_far:.2f})이 상한(${max_cost_usd:.2f})을 초과해 중단합니다.")
                stop_reason = "cost_limit_exceeded"
                break

    success = done_summary is not None
    if stop_reason is None and not success:
        stop_reason = "max_steps_reached"
        log_fn(f"[에이전틱] 최대 스텝({max_steps})에 도달했지만 done을 선언하지 않았습니다.")

    return {
        "success": success,
        "reason": "done" if success else stop_reason,
        "steps_taken": steps_taken,
        "files_touched": files_touched,
        "summary": done_summary,
    }
