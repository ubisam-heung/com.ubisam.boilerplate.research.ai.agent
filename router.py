"""작업을 로컬 LLM / 외부 도구(Codex, Claude Code)로 분배하는 라우터"""

from harness import project_guide

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


ROUTING_PROMPT = """다음 코딩 작업을 분석해서 JSON으로만 답해라. 다른 말은 하지 마라.

{guide}작업: {task}
관련 파일 수: {file_count}
예상 컨텍스트 토큰 수: {est_tokens}

판단 기준:
- 단순 버그 수정, 작은 함수 추가/수정, 1~3개 파일 범위 -> "local"
- 대규모 리팩토링, 아키텍처 설계, 멀티파일 의존성 분석,
  복잡한 알고리즘, 매우 큰 컨텍스트가 필요한 작업 -> "external"

JSON 형식 (이 형식 그대로):
{{"decision": "local", "reason": "...", "tool": null}}
또는
{{"decision": "external", "reason": "...", "tool": "claude_code"}}
"""


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


def pre_filter(task: str, file_count: int, est_tokens: int, cfg: dict) -> str | None:
    """규칙 기반 1차 필터. 명확한 경우 LLM 호출 없이 즉시 결정."""
    routing_cfg = cfg.get("routing", {})
    max_files = routing_cfg.get("max_local_files", 5)
    max_tokens = routing_cfg.get("max_local_tokens", 32000)
    keywords = routing_cfg.get("force_external_keywords", [])

    if any(kw in task for kw in keywords):
        return "external"
    if est_tokens > max_tokens or file_count > max_files:
        return "external"
    if est_tokens < 4000 and file_count <= 2:
        return "local"
    return None


class Router:
    def __init__(self, llm, cfg: dict, guide: str = ""):
        self.llm = llm
        self.cfg = cfg
        self.guide = guide

    def decide(self, task: str, file_count: int, est_tokens: int) -> dict:
        pre = pre_filter(task, file_count, est_tokens, self.cfg)
        if pre == "local":
            return {"decision": "local", "reason": "규칙 기반: 작고 명확한 작업", "tool": None}
        if pre == "external":
            default_tool = pick_external_tool(self.cfg)
            return {"decision": "external", "reason": "규칙 기반: 큰 작업/키워드 매칭", "tool": default_tool}

        # 애매한 경우 LLM 판단
        prompt = ROUTING_PROMPT.format(
            guide=project_guide.as_prelude(self.guide),
            task=task, file_count=file_count, est_tokens=est_tokens,
        )
        try:
            result = self.llm.generate(prompt, json_mode=True)
        except Exception:
            # 실패 시 안전하게 외부로
            default_tool = pick_external_tool(self.cfg)
            return {"decision": "external", "reason": "라우팅 LLM 호출 실패, 안전하게 외부 도구 사용", "tool": default_tool}

        if result.get("decision") not in ("local", "external"):
            default_tool = pick_external_tool(self.cfg)
            return {"decision": "external", "reason": "라우팅 응답 형식 오류, 안전하게 외부 도구 사용", "tool": default_tool}

        if result["decision"] == "external":
            # LLM이 비활성화된 도구를 골랐을 수 있으니 활성화된 도구로 보정
            result["tool"] = pick_external_tool(self.cfg, preferred=result.get("tool"))
        return result
