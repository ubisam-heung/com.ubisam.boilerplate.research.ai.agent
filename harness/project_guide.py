"""프로젝트별 지침(AGENTS.md) 로딩.

사용자가 work_root에 둔 AGENTS.md(없으면 CLAUDE.md)를 읽어, 로컬 파이프라인의
파일 선택·계획·라우팅 프롬프트 앞에 붙일 컨텍스트 블록으로 만든다.
외부 도구(Claude Code/Codex)는 자체 관례로 이 파일을 읽지만, 로컬 LLM은
직접 주입해줘야 프로젝트 구조/컨벤션/금지 사항을 알 수 있다.
"""
import os

# work_root 기준으로 이 순서대로 처음 발견된 파일 하나만 사용한다.
GUIDE_FILENAMES = ("AGENTS.md", "CLAUDE.md")

# 프롬프트에 통째로 싣기엔 너무 길 수 있으니 상한을 둔다(글자 수).
MAX_GUIDE_CHARS = 6000


def _read_one(root: str) -> str:
    """한 디렉토리에서 GUIDE_FILENAMES 중 처음 발견되는 파일을 읽는다. 없으면 빈 문자열."""
    for name in GUIDE_FILENAMES:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except (OSError, UnicodeDecodeError):
            return ""
    return ""


def load(roots) -> str:
    """주어진 디렉토리들의 AGENTS.md를 읽어 합쳐 반환한다.

    roots는 단일 경로(str) 또는 경로 리스트. **work_root(workspace) 안이 아니라
    설치/프로젝트 루트의 AGENTS.md를 본다.** 여러 루트를 주면(예: [전역 ai-agent 폴더,
    프로젝트 루트]) 각각의 지침을 순서대로 이어붙여 layering 한다(전역 + 프로젝트별 추가).
    중복 경로는 한 번만, 총 길이는 MAX_GUIDE_CHARS로 제한한다.
    """
    if isinstance(roots, str):
        roots = [roots]
    seen = set()
    parts = []
    for root in roots:
        if not root:
            continue
        key = os.path.realpath(root)
        if key in seen:
            continue
        seen.add(key)
        text = _read_one(root)
        if text:
            parts.append(text)
    combined = "\n\n".join(parts)
    if len(combined) > MAX_GUIDE_CHARS:
        combined = combined[:MAX_GUIDE_CHARS] + "\n…(생략)"
    return combined


def as_prelude(guide: str) -> str:
    """지침을 프롬프트 맨 앞에 붙일 블록으로 감싼다. 빈 지침이면 빈 문자열."""
    if not guide:
        return ""
    return (
        "다음은 이 프로젝트의 지침(AGENTS.md)이다. 작업·판단 시 반드시 반영해라:\n"
        "<project_guide>\n"
        f"{guide}\n"
        "</project_guide>\n\n"
    )
