"""잡담 판별 및 외부 도구(Codex, Claude Code) 선택 유틸.

local_llm/openrouter는 이제 local/external로 나뉘지 않고 항상
harness/agentic_loop.py 하나로 실행되므로, 여기서는 "작업이 아닌 잡담인지"
판별하는 것과 실패 시 위임할 외부 CLI를 고르는 것만 담당한다.
"""

CHATTER_CHECK_PROMPT = """다음 입력이 코딩/파일 수정/개발 작업인지, 아니면 그 외(인사, 일상대화, 수학 계산, 잡담 등)인지 판단해라. JSON으로만 답해라.

입력: {task}

{{"is_task": true}}  ← 코딩/파일/개발 관련 작업
{{"is_task": false}} ← 그 외 모든 것"""


CHATTER_REPLY_PROMPT = """사용자가 일상적인 말을 건넸다. 코딩 에이전트지만 친근하게 한두 문장으로 답하고, 개발 작업이 있으면 말해달라고 자연스럽게 마무리해라.

사용자 입력: {task}"""


_OBVIOUS_CHATTER = {
    "안녕", "안녕하세요", "하이", "hi", "hello", "hey",
    "고마워", "감사", "감사합니다", "thanks", "thank you",
    "ㅇㅋ", "오케이", "ok", "okay", "넵", "네", "응",
    "잘가", "바이", "bye",
}


def _is_obvious_chatter(task: str) -> bool:
    """짧고 명확한 인사/감사/응답은 LLM 판정 전에 잡담으로 확정한다."""
    text = (task or "").strip().lower()
    if not text:
        return True
    normalized = text.rstrip("!?.。！？~ \t")
    return normalized in _OBVIOUS_CHATTER


def is_chatter(llm, task: str) -> bool:
    """코딩 작업이 아닌 일상대화/잡담이면 True를 반환한다."""
    if _is_obvious_chatter(task):
        return True
    try:
        result = llm.generate(CHATTER_CHECK_PROMPT.format(task=task), json_mode=True, num_predict=32)
        return not result.get("is_task", True)
    except Exception:
        return False


def reply_chatter(llm, task: str) -> str:
    """일상대화에 친근하게 짧게 답한다."""
    try:
        return llm.generate(CHATTER_REPLY_PROMPT.format(task=task), num_predict=150)
    except Exception:
        return "안녕하세요! 개발 관련 작업이 있으면 말씀해주세요."


def tool_enabled(cfg: dict, tool: str) -> bool:
    """external_tools.<tool>.enabled (기본 true)."""
    return cfg.get("external_tools", {}).get(tool, {}).get("enabled", True)


def pick_external_tool(cfg: dict, preferred: str | None = None) -> str | None:
    """활성화된(enabled) 외부 도구 하나를 고른다.

    preferred -> external_tools.default -> 나머지 등록된 도구 순으로 시도하고,
    활성화된 도구가 하나도 없으면 None을 반환한다.
    """
    ext = cfg.get("external_tools", {})
    order = []
    if preferred:
        order.append(preferred)
    default = ext.get("default", "claude_code")
    if default not in order:
        order.append(default)
    for name in ("claude_code", "codex"):
        if name not in order:
            order.append(name)
    for name in order:
        if tool_enabled(cfg, name):
            return name
    return None
