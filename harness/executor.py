"""diff 생성 및 파일에 변경사항 적용 (백업 포함)

기존 파일 수정은 SEARCH/REPLACE 블록으로 받아 적용한다(파일 전체를 다시 쓰지 않음).
큰 파일에서 모델이 관련 없는 부분까지 다시 쓰다가 실수하는 것을 막고, 토큰도 아낀다.
새 파일 생성만 전체 내용을 그대로 받는다.
"""
import difflib
import os
import re
import shutil


class PathEscapeError(ValueError):
    """work_root 밖으로 벗어나는 경로(절대경로·.. 탈출)를 적용하려 할 때."""


class BlockApplyError(ValueError):
    """SEARCH 블록이 현재 파일 내용과 일치하지 않아 적용할 수 없을 때."""


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


_SEARCH_MARK = "<<<<<<< SEARCH"
_DIVIDER_MARK = "======="
_REPLACE_MARK = ">>>>>>> REPLACE"

_BLOCK_RE = re.compile(
    re.escape(_SEARCH_MARK) + r"\n(.*?)\n" + re.escape(_DIVIDER_MARK) + r"\n(.*?)\n" + re.escape(_REPLACE_MARK),
    re.DOTALL,
)

CHANGE_PROMPT = """파일: {filepath}

현재 내용:
```
{current_content}
```

작업 설명: {description}

위 작업을 반영해 변경할 부분만 SEARCH/REPLACE 블록으로 출력해라.
파일 전체를 다시 쓰지 마라. 블록마다 SEARCH 안의 내용은 "현재 내용"과
공백 하나까지 정확히 일치해야 한다. 여러 군데를 바꿔야 하면 블록을 여러 개 써라.

형식 (설명, 코드펜스 없이 이 형식 그대로):
<<<<<<< SEARCH
(바꿀 부분의 기존 코드, 여러 줄 가능)
=======
(새 코드)
>>>>>>> REPLACE

파일 전체가 새로 작성되어야 할 정도로 크게 바뀐다면 SEARCH 블록에 파일
전체 내용을, REPLACE 블록에 새 전체 내용을 넣어도 된다.
"""

NEW_FILE_PROMPT = """새 파일: {filepath}

작업 설명: {description}

위 작업을 반영한 파일 전체 내용을 출력해라.
설명, 코드펜스(```), 마크다운 없이 순수 파일 내용만 출력해라.
"""


def generate_change(llm, filepath: str, current_content: str, description: str) -> tuple[str, str]:
    """새 파일 내용과 unified diff 문자열을 반환.

    기존 파일: SEARCH/REPLACE 블록을 받아 current_content에 적용한다.
    새 파일(현재 내용 없음): 전체 내용을 그대로 받는다.
    """
    is_new_file = not current_content
    if is_new_file:
        prompt = NEW_FILE_PROMPT.format(filepath=filepath, description=description)
        new_content = llm.generate(prompt, num_predict=8192)
        new_content = _strip_code_fence(new_content)
    else:
        prompt = CHANGE_PROMPT.format(filepath=filepath, current_content=current_content, description=description)
        raw = llm.generate(prompt, num_predict=8192)
        new_content = apply_blocks(current_content, raw)

    diff = difflib.unified_diff(
        current_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
    )
    return new_content, "".join(diff)


def parse_blocks(raw: str) -> list[tuple[str, str]]:
    """모델 응답에서 (search, replace) 쌍 목록을 뽑아낸다. 블록이 없으면 빈 리스트."""
    text = _strip_code_fence(raw)
    return [(search, replace) for search, replace in _BLOCK_RE.findall(text)]


def apply_blocks(current_content: str, raw: str) -> str:
    """SEARCH/REPLACE 블록들을 current_content에 순서대로 적용한 결과를 반환한다.

    각 SEARCH는 현재까지 누적된 내용에서 정확히 한 번 일치해야 한다.
    블록을 하나도 못 찾으면(모델이 형식을 안 지킨 경우) raw 전체를 파일
    전체 내용으로 간주해 그대로 반환한다(구형 전체-재작성 응답과의 호환).
    """
    blocks = parse_blocks(raw)
    if not blocks:
        return _strip_code_fence(raw)

    content = current_content
    for search, replace in blocks:
        if search == "":
            content = content + replace
            continue
        count = content.count(search)
        if count == 0:
            raise BlockApplyError(f"SEARCH 블록이 파일 내용과 일치하지 않습니다:\n{search[:300]}")
        if count > 1:
            raise BlockApplyError(f"SEARCH 블록이 파일에서 {count}번 일치해 어느 곳인지 특정할 수 없습니다:\n{search[:300]}")
        content = content.replace(search, replace, 1)
    return content


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
