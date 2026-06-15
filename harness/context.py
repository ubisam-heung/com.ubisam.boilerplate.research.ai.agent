"""작업과 관련된 파일을 탐색/선택"""
import os

from harness import project_guide

# 작업이 '새 파일 생성'을 명시할 때만 존재하지 않는 경로를 허용한다.
# (그 외엔 로컬 모델이 키워드로 파일을 날조하는 것을 막는다)
_CREATE_HINTS = (
    "새 파일", "새로운 파일", "파일을 만들", "파일 생성", "파일을 생성",
    "새로 만들", "새로 생성", "create file", "new file", "scaffold", "스캐폴드",
)


def _wants_new_file(task: str) -> bool:
    t = (task or "").lower()
    return any(h.lower() in t for h in _CREATE_HINTS)

FILE_SELECTION_PROMPT = """{guide}다음은 프로젝트의 파일 목록이다:
{tree}

작업: {task}

이 작업을 수행하기 위해 읽어야 할 파일을 최대 5개까지 선택해라.
존재하는 파일 경로 중에서만 선택해라. 새로 만들어야 할 파일이 있다면 포함해도 된다.
JSON 배열로만 답해라. 예: ["src/api/routes.py", "src/models/user.py"]
"""


def get_project_tree(root: str = ".", exclude_dirs: list[str] | None = None, max_files: int = 500) -> str:
    exclude_dirs = set(exclude_dirs or [])
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs and not d.startswith(".")]
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            paths.append(rel)
            if len(paths) >= max_files:
                return "\n".join(paths)
    return "\n".join(paths)


def select_relevant_files(llm, task: str, root: str = ".", exclude_dirs: list[str] | None = None,
                          guide: str = "") -> list[str]:
    tree = get_project_tree(root, exclude_dirs)
    if not tree:
        return []
    prompt = FILE_SELECTION_PROMPT.format(guide=project_guide.as_prelude(guide), tree=tree, task=task)
    try:
        result = llm.generate(prompt, json_mode=True)
    except Exception:
        return []

    if isinstance(result, dict):
        # 모델이 {"files": [...]} 형태로 반환할 수도 있음
        result = result.get("files", [])
    if not isinstance(result, list):
        return []
    selected = [_resolve_path(root, tree, str(f)) for f in result][:5]

    # 존재하지 않는 경로는 버린다 — 단, 작업이 '새 파일 생성'을 명시한 경우엔 허용.
    # 로컬 모델이 'tableOptions.js' 같은 파일을 키워드만 보고 날조하는 것을 차단한다.
    allow_new = _wants_new_file(task)
    out = []
    for f in selected:
        if os.path.exists(os.path.join(root, f)) or allow_new:
            out.append(f)
    return out


def _resolve_path(root: str, tree: str, name: str) -> str:
    """모델이 고른 경로를 실제 트리 경로로 보정한다.

    로컬 모델이 풀 경로 대신 파일명만(예: 'limsAlarms2.vue') 반환하면, 그대로는
    work_root 루트에 없는 파일이라 '새 파일'로 오인돼 가짜로 생성된다.
    work_root에 실제로 존재하면 그대로 두고, 없을 때만 트리에서 같은 파일명을
    찾아 유일하게 매칭되면 그 경로로 바꾼다. 모호하거나 없으면 원본 유지(=새 파일 의도).
    """
    name = name.strip().lstrip("./")
    if os.path.exists(os.path.join(root, name)):
        return name
    base = os.path.basename(name)
    matches = [p for p in tree.split("\n") if p and os.path.basename(p) == base]
    if len(matches) == 1:
        return matches[0]
    return name


def read_files(root: str, files: list[str]) -> dict[str, str]:
    contents = {}
    for f in files:
        path = os.path.join(root, f)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                contents[f] = fp.read()
        except (FileNotFoundError, UnicodeDecodeError):
            contents[f] = ""  # 새 파일이거나 읽을 수 없는 파일
    return contents


def estimate_tokens(file_contents: dict[str, str]) -> int:
    total_chars = sum(len(c) for c in file_contents.values())
    return total_chars // 3
