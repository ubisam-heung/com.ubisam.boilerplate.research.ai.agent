"""작업과 관련된 파일을 탐색/선택"""
import os
import re

from harness import project_guide

_CONVERSATION_HEADER = "직전 대화(최근 순서대로, 참고용 — 현재 작업이 여기서 이어지는 것일 수 있다):"


def format_conversation_history(conversation_history: list[str] | None, limit: int = 5) -> str:
    """대화형 세션의 직전 턴 입력들을 프롬프트에 얹을 짧은 텍스트로 변환한다.

    "왜?", "그건 왜 안 돼?" 같은 후속 질문은 직전 턴을 참조해야 답이 나오는데,
    이 컨텍스트가 없으면 모델이 매 턴을 독립된 새 질문으로 오해해 엉뚱한 답을
    지어낸다(예: 관련 없는 파일을 억지로 찾아 답변). agentic_loop.run_loop와
    agent.explain_task 양쪽 모두 이 헬퍼로 동일하게 포맷팅해, 코드 수정 경로와
    설명 모드 경로 사이에 컨텍스트 유무가 갈리지 않게 한다.
    """
    if not conversation_history:
        return ""
    recent = conversation_history[-limit:]
    lines = "\n".join(f"- {t}" for t in recent)
    return f"\n{_CONVERSATION_HEADER}\n{lines}\n"

# 작업 문자열에서 뽑아낼 "식별자성" 토큰: 코드/티켓 ID(G3-MSG-02), 함수명, 파일명 조각 등.
# 파일명에는 없고 파일 "내용" 안에만 있는 키워드를 찾기 위한 grep 사전 필터에 쓴다.
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}(?:-\d+)?|\d{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "파일", "확인", "구현", "정상",
    "적으로", "되어있는지", "해줘", "번이", "테스트",
}

# grep 후보 스코어링 시 확장자별 가중치. 소스 코드는 우대, 서드파티 고지/문서/
# 패키지 메타데이터는 감점해서 "License" 같은 흔한 단어가 코드보다 우선되는 걸 막는다.
_SOURCE_EXT_BONUS = {
    ".cs": 3, ".py": 3, ".ts": 3, ".tsx": 3, ".js": 3, ".jsx": 3, ".java": 3,
    ".go": 3, ".rs": 3, ".cpp": 3, ".c": 3, ".h": 3, ".hpp": 3,
}
_DOC_EXT_PENALTY = {".txt": -5, ".md": -1, ".json": -1, ".xml": -1, ".config": -1,
                    ".csproj": -3, ".sln": -3, ".vcxproj": -3}
# 매칭 개수와 무관하게 항상 후순위로 미는 경로 패턴 (서드파티 고지, 패키지 메타데이터,
# 빌드 산출물 등 — 소스 코드가 아니라 grep 매칭 수만 부풀리는 경로들)
_LOW_PRIORITY_PATH_MARKERS = (
    "third-party-notices", "thirdpartynotices", "license.txt", "licenses.txt",
    os.sep + "packages" + os.sep, os.sep + "node_modules" + os.sep,
    os.sep + "bin" + os.sep, os.sep + "obj" + os.sep,
)
# 흔히 벤더링되어 들어오는 서드파티 프론트엔드 라이브러리 파일명 패턴. "Async",
# "Error", "State" 같은 흔한 단어는 jquery/bootstrap 등의 소스에도 매우 자주
# 등장해 실제 우리 코드보다 매칭이 많아지기 쉽다. .js/.ts라서 _SOURCE_EXT_BONUS로
# 오히려 가점을 받는 역효과가 있었으므로 파일명 패턴으로 따로 걸러낸다.
_VENDORED_LIB_RE = re.compile(
    r"(^|[/\\])(jquery|bootstrap|lodash|moment|react|vue|angular|popper|"
    r"chart|d3|axios|underscore)[.\-]?[\w.\-]*\.(min\.)?js$",
    re.IGNORECASE,
)


def _path_score_bonus(rel_path: str) -> int:
    """파일 경로만 보고 매기는 가점/감점. 확장자 + 저우선순위 경로 패턴."""
    lower = rel_path.lower()
    bonus = 0
    _, ext = os.path.splitext(lower)
    bonus += _SOURCE_EXT_BONUS.get(ext, 0)
    bonus += _DOC_EXT_PENALTY.get(ext, 0)
    if any(marker in (os.sep + lower) for marker in _LOW_PRIORITY_PATH_MARKERS):
        bonus -= 100
    if _VENDORED_LIB_RE.search(lower):
        bonus -= 100
    return bonus

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
{conversation}
작업: {task}

이 작업을 수행하기 위해 읽어야 할 파일을 최대 5개까지 선택해라.
존재하는 파일 경로 중에서만 선택해라. 새로 만들어야 할 파일이 있다면 포함해도 된다.
작업이 직전 대화를 이어받는 후속 질문(예: "왜?")이면, 그 직전 대화가 다루던 파일/주제와
관련된 파일을 우선 고려해라 — 작업 문장 자체의 키워드만 보고 무관한 파일을 고르지 마라.
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


def _extract_keywords(task: str) -> list[str]:
    """작업 문자열에서 grep에 쓸 만한 식별자성 토큰을 뽑는다.

    'g3-msg-02 번이 구현이 정상적으로 되어있는지' 같은 문장에서 'g3-msg-02'처럼
    파일명이 아니라 파일 "내용"에만 등장할 법한 코드/티켓 ID를 잡아내는 게 목적이다.
    """
    tokens = _IDENTIFIER_RE.findall(task or "")
    out, seen = [], set()
    for t in tokens:
        low = t.lower()
        if low in _STOPWORDS or len(t) < 3:
            continue
        if low not in seen:
            seen.add(low)
            out.append(t)
    return out


def grep_matching_files(task: str, root: str = ".", exclude_dirs: list[str] | None = None,
                        max_files_scanned: int = 2000, max_matches: int = 5) -> list[str]:
    """작업 문자열에서 뽑은 키워드로 파일 "내용"을 직접 검색해 후보를 찾는다.

    select_relevant_files는 파일명 목록만 보고 LLM이 추측하는 방식이라, 식별자가
    파일명이 아니라 본문에만 있으면(예: 마크다운 문서 안의 'G3-MSG-02') 못 찾는다.
    여기서는 실제로 파일을 열어 grep해서 이런 경우를 잡는다. 매칭 개수가 많은
    파일부터 우선한다. exclude_dirs 밖 텍스트 파일만 대상으로 하며, 바이너리/디코드
    실패 파일은 조용히 건너뛴다.
    """
    keywords = _extract_keywords(task)
    if not keywords:
        return []
    exclude_dirs = set(exclude_dirs or [])
    patterns = [re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords]

    scored = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs and not d.startswith(".")]
        for fname in filenames:
            if scanned >= max_files_scanned:
                break
            scanned += 1
            full = os.path.join(dirpath, fname)
            try:
                with open(full, "r", encoding="utf-8") as fp:
                    text = fp.read()
            except (UnicodeDecodeError, OSError):
                continue
            hits = sum(len(p.findall(text)) for p in patterns)
            if hits > 0:
                rel = os.path.relpath(full, root)
                # 매칭 횟수를 30건에서 캡해 "흔한 단어가 반복되는 대용량 문서"가
                # 매칭 1~2회뿐인 진짜 관련 코드를 순전히 개수로 짓누르지 못하게 한다.
                score = min(hits, 30) + _path_score_bonus(rel)
                scored.append((score, rel))

    scored.sort(key=lambda x: -x[0])
    return [rel for _, rel in scored[:max_matches]]


def select_relevant_files(llm, task: str, root: str = ".", exclude_dirs: list[str] | None = None,
                          guide: str = "", conversation_history: list[str] | None = None) -> list[str]:
    tree = get_project_tree(root, exclude_dirs)
    if not tree:
        return []

    # 1) 실제 grep으로 내용 안에 키워드가 있는 파일부터 찾는다 (가장 확실한 신호).
    grep_hits = grep_matching_files(task, root, exclude_dirs)

    conv_text = format_conversation_history(conversation_history)
    prompt = FILE_SELECTION_PROMPT.format(
        guide=project_guide.as_prelude(guide), tree=tree, task=task, conversation=conv_text,
    )
    try:
        result = llm.generate(prompt, json_mode=True)
    except Exception:
        result = []

    if isinstance(result, dict):
        # 모델이 {"files": [...]} 형태로 반환할 수도 있음
        result = result.get("files", [])
    if not isinstance(result, list):
        result = []
    llm_selected = [_resolve_path(root, tree, str(f)) for f in result]

    # grep 히트를 우선 배치하고, LLM이 고른 나머지로 5개를 채운다.
    combined, seen = [], set()
    for f in grep_hits + llm_selected:
        if f not in seen:
            seen.add(f)
            combined.append(f)
    selected = combined[:5]

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
