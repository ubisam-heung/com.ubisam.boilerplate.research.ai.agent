"""diff 생성 및 파일에 변경사항 적용 (백업 포함)"""
import difflib
import os
import shutil


class PathEscapeError(ValueError):
    """work_root 밖으로 벗어나는 경로(절대경로·.. 탈출)를 적용하려 할 때."""


def safe_full_path(root: str, filepath: str) -> str:
    """root 안의 절대 경로를 돌려준다. root 밖으로 벗어나면 PathEscapeError.

    로컬 모델이 '/contents/x.vue' 같은 절대경로나 '../..' 탈출 경로를 뱉어도
    work_root 밖(시스템 루트 등)에 쓰지 못하게 막는 하드 가드레일.
    """
    root_abs = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_abs, filepath))
    if full != root_abs and not full.startswith(root_abs + os.sep):
        raise PathEscapeError(filepath)
    return full

CHANGE_PROMPT = """파일: {filepath}

현재 내용:
```
{current_content}
```

작업 설명: {description}

위 작업을 반영한 파일 전체 내용을 출력해라.
설명, 코드펜스(```), 마크다운 없이 순수 파일 내용만 출력해라.
"""


def generate_change(llm, filepath: str, current_content: str, description: str) -> tuple[str, str]:
    """새 파일 내용과 unified diff 문자열을 반환"""
    current_display = current_content if current_content else "(새 파일)"
    prompt = CHANGE_PROMPT.format(filepath=filepath, current_content=current_display, description=description)
    new_content = llm.generate(prompt, num_predict=8192)
    new_content = _strip_code_fence(new_content)

    diff = difflib.unified_diff(
        current_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
    )
    return new_content, "".join(diff)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # 첫 줄(```python 등)과 마지막 ``` 제거
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def apply_change(root: str, filepath: str, new_content: str, backup_dir: str = ".agent_backup") -> str | None:
    """변경 적용. 기존 파일이 있으면 backup_dir에 백업하고 백업 경로를 반환.

    work_root 밖 경로면 PathEscapeError를 던지고 아무것도 쓰지 않는다.
    """
    full_path = safe_full_path(root, filepath)
    backup_path = None

    if os.path.exists(full_path):
        backup_root = os.path.join(root, backup_dir)
        os.makedirs(backup_root, exist_ok=True)
        backup_path = os.path.join(backup_root, filepath.replace(os.sep, "__") + ".bak")
        shutil.copy2(full_path, backup_path)

    os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return backup_path


def restore_backup(root: str, filepath: str, backup_path: str):
    full_path = safe_full_path(root, filepath)
    shutil.copy2(backup_path, full_path)
