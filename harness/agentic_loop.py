"""도구 호출 기반 반복 루프(에이전틱 루프).

기존 파이프라인(planner→executor 1회성)과 달리, 모델이 매 턴마다
"다음에 뭘 할지"를 도구 호출 하나로 스스로 고르고, 그 결과를 다시
보여준 뒤 또 고르게 하는 것을 모델이 done을 선언하거나 최대 스텝
수에 닿을 때까지 반복한다. "기능 개발" 같은 다단계 작업에서
1회성 파이프라인이 계획을 재검토하지 못하는 한계를 메운다.
"""
import json
import os
import re

from harness import context, hooks, verifier
from harness.executor import (
    BlockApplyError, PathEscapeError, apply_blocks, apply_change, safe_full_path,
)

MAX_STEPS_DEFAULT = 20
_MAX_TOOL_OUTPUT_CHARS = 4000
_MAX_READ_CHARS = 12000

SYSTEM_PROMPT = """당신은 코딩 에이전트다. 아래 도구를 매 턴 하나씩 호출해서
작업을 완료해라. 파일 내용을 추측하지 말고, 필요하면 반드시 read_file이나
grep으로 먼저 확인해라.

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


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit}자 생략)"


def _dispatch(action: dict, work_root: str, hook_roots, cfg: dict, log_fn) -> tuple[str, bool]:
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
        timeout = cfg.get("harness", {}).get("verify_timeout_sec", 120)
        results = verifier.run_verification([command], work_root, timeout)
        r = results[0]
        status = "성공(exit 0)" if r["success"] else f"실패(exit {r['returncode']})"
        return f"{status}\n{_truncate(r['output'], _MAX_TOOL_OUTPUT_CHARS)}", False

    if tool == "done":
        summary = action.get("summary", "(요약 없음)")
        log_fn(f"  [완료 선언] {summary}")
        return summary, True

    return f"오류: 알 수 없는 도구입니다: {tool!r}. list_dir/grep/read_file/write_file/run_command/done 중 하나를 써라.", False


def run_loop(llm, task: str, work_root: str, hook_roots, cfg: dict, log_fn,
             guide: str = "", max_steps: int | None = None) -> dict:
    """에이전틱 루프를 실행한다.

    모델이 매 스텝 도구 호출 JSON을 하나 반환하면 실행하고 결과를 이력에
    쌓아 다음 프롬프트에 넘긴다. done 호출 또는 max_steps 도달 시 종료.
    """
    max_steps = max_steps or cfg.get("harness", {}).get("agentic_max_steps", MAX_STEPS_DEFAULT)
    system = SYSTEM_PROMPT.format(
        task=task, work_root=work_root,
        guide=f"\n프로젝트 지침:\n{guide}\n" if guide else "",
    )

    history_entries = []
    steps_taken = 0
    files_touched = []
    done_summary = None

    for step in range(1, max_steps + 1):
        history_text = "".join(history_entries) or "(아직 없음)"
        prompt = TURN_PROMPT.format(history=history_text)
        try:
            action = llm.generate(prompt, system=system, json_mode=True, num_predict=1024)
        except Exception as exc:
            log_fn(f"[에이전틱] 스텝 {step}: 모델 호출 실패 - {exc}")
            history_entries.append(_fmt_step(step, {"tool": "?"}, f"오류: 모델 호출 실패 - {exc}"))
            steps_taken = step
            continue

        if not isinstance(action, dict) or "tool" not in action:
            log_fn(f"[에이전틱] 스텝 {step}: 잘못된 응답 형식 - {action!r}")
            history_entries.append(_fmt_step(step, {"tool": "?"}, "오류: 도구 호출 JSON 형식이 아닙니다. {\"tool\": ...} 형식으로 답해라."))
            steps_taken = step
            continue

        log_fn(f"[에이전틱] 스텝 {step}: {action.get('tool')} {action.get('path') or action.get('pattern') or action.get('command') or ''}")
        result, finished = _dispatch(action, work_root, hook_roots, cfg, log_fn)
        steps_taken = step

        if action.get("tool") == "write_file" and "오류" not in result and "차단" not in result:
            path = action.get("path")
            if path and path not in files_touched:
                files_touched.append(path)

        history_entries.append(_fmt_step(step, action, _truncate(result, _MAX_TOOL_OUTPUT_CHARS)))
        # 이력이 너무 길어지지 않게 최근 N개만 유지 (오래된 스텝은 요약 없이 버림)
        if len(history_entries) > 12:
            history_entries = history_entries[-12:]

        if finished:
            done_summary = result
            break

    success = done_summary is not None
    if not success:
        log_fn(f"[에이전틱] 최대 스텝({max_steps})에 도달했지만 done을 선언하지 않았습니다.")

    return {
        "success": success,
        "reason": "done" if success else "max_steps_reached",
        "steps_taken": steps_taken,
        "files_touched": files_touched,
        "summary": done_summary,
    }
