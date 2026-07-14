"""검증 실패 시 자동 복구 루프"""
from harness.executor import _strip_code_fence, apply_change

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

위 오류를 분석하고 수정한 파일 전체 내용을 출력해라.
설명, 코드펜스, 마크다운 없이 순수 파일 내용만 출력해라.
"""


def recover_file(llm, root: str, filepath: str, current_content: str, failed_result: dict) -> str:
    """LLM에게 오류를 보여주고 수정된 새 내용을 받아 적용한 뒤, 새 내용을 반환"""
    prompt = RECOVERY_PROMPT.format(
        filepath=filepath,
        current_content=current_content,
        cmd=failed_result["cmd"],
        error_output=failed_result["output"][:4000],  # 너무 길면 자름
    )
    fixed = llm.generate(prompt, num_predict=8192)
    fixed = _strip_code_fence(fixed)
    apply_change(root, filepath, fixed)
    return fixed
