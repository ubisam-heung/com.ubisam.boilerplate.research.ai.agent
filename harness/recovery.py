"""검증 실패 시 자동 복구 루프"""
from harness.executor import BlockApplyError, apply_blocks, apply_change

RECOVERY_PROMPT = """이전 수정이 검증에 실패했다.

파일: {filepath}

현재(실패한) 파일 내용:
```
{current_content}
```

검증 명령: {cmd}
오류 출력:
```
{error_output}
```
{history}
위 오류를 분석하고, 바꿀 부분만 SEARCH/REPLACE 블록으로 출력해라.
파일 전체를 다시 쓰지 마라. SEARCH 안의 내용은 "현재(실패한) 파일 내용"과
공백 하나까지 정확히 일치해야 한다.

형식 (설명, 코드펜스 없이 이 형식 그대로):
<<<<<<< SEARCH
(바꿀 부분의 기존 코드)
=======
(새 코드)
>>>>>>> REPLACE
"""

_HISTORY_HEADER = "\n이전 복구 시도 이력 (같은 실수를 반복하지 마라):\n"


def _format_history(history: list[dict]) -> str:
    """이전 복구 시도들의 (시도 diff, 결과 오류)를 프롬프트에 넣을 텍스트로 만든다."""
    if not history:
        return ""
    parts = [_HISTORY_HEADER]
    for i, h in enumerate(history, 1):
        parts.append(
            f"--- 시도 {i} ---\n적용한 변경:\n{h['diff'] or '(변경 없음)'}\n"
            f"결과 오류:\n{h['error'][:1000]}\n"
        )
    return "\n".join(parts) + "\n"


def recover_file(llm, root: str, filepath: str, current_content: str, failed_result: dict,
                  history: list[dict] | None = None) -> tuple[str, str]:
    """LLM에게 오류를 보여주고 SEARCH/REPLACE로 수정본을 받아 적용한 뒤, (새 내용, diff)를 반환.

    history: 이전 복구 시도 이력 [{"diff": ..., "error": ...}, ...]. 누적해서 넘기면
    모델이 직전에 뭘 시도했다가 왜 실패했는지 보고 같은 실수를 반복하지 않는다.
    """
    prompt = RECOVERY_PROMPT.format(
        filepath=filepath,
        current_content=current_content,
        cmd=failed_result["cmd"],
        error_output=failed_result["output"][:4000],  # 너무 길면 자름
        history=_format_history(history or []),
    )
    raw = llm.generate(prompt, num_predict=8192)
    try:
        fixed = apply_blocks(current_content, raw)
    except BlockApplyError:
        # SEARCH가 어긋나면 응답 전체를 파일 전체로 간주해 폴백
        from harness.executor import _strip_code_fence
        fixed = _strip_code_fence(raw)

    import difflib
    diff = "".join(difflib.unified_diff(
        current_content.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile=f"a/{filepath}", tofile=f"b/{filepath}",
    ))
    apply_change(root, filepath, fixed)
    return fixed, diff
