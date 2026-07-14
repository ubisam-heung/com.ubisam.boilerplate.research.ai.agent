"""SEARCH/REPLACE 블록 적용 및 파일 변경 적용 (백업 포함)

harness/agentic_loop.py의 write_file 도구가 이 모듈의 apply_blocks/apply_change/
safe_full_path를 사용해 SEARCH/REPLACE 블록을 파일에 반영한다.
"""
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
