"""잡담 판별, 외부 도구(Codex, Claude Code) 선택, OpenRouter 모델 자동 선택 유틸.

local_llm/openrouter는 이제 local/external로 나뉘지 않고 항상
harness/agentic_loop.py 하나로 실행되므로, 여기서는 "작업이 아닌 잡담인지"
판별하는 것, 실패 시 위임할 외부 CLI를 고르는 것, 그리고 openrouter.auto_model이
켜졌을 때 작업 복잡도에 맞는 모델을 고르는 것을 담당한다.
"""
import re

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


# --- OpenRouter 모델 자동 선택 (작업 복잡도 기반) -----------------------------
#
# 휴리스틱(규칙 기반)으로 점수를 매긴다 — 추가 LLM 호출 없이 빠르고 무료다.
# 점수가 높을수록 "어려운 작업"으로 보고, config.yaml의 openrouter.models
# 목록에서 max_score가 이 점수 이상인 것 중 가장 저렴한(목록 순서상 가장
# 앞쪽) 모델을 고른다 — 목록은 반드시 싼→비싼(약한→강한) 순으로 적어야 한다.

# 어려움 신호: 여러 파일/구조 변경, 버그 추적, 테스트/검증이 필요한 작업.
_HARD_KEYWORDS = (
    "리팩토", "마이그레이션", "아키텍처", "설계", "전체", "모든 파일", "여러 파일",
    "동시성", "race condition", "레이스 컨디션", "성능", "최적화", "보안", "취약점",
    "버그", "디버그", "원인", "실패", "에러", "오류", "테스트 작성", "통합 테스트",
    "동기화", "스레드", "async", "비동기", "algorithm", "알고리즘",
    "refactor", "migrate", "architecture", "concurrency", "optimi", "security",
    "vulnerab", "debug", "root cause",
)
# 쉬움 신호: 단순 조회/사소한 텍스트 변경.
_EASY_KEYWORDS = (
    "오타", "이름만", "이름 변경", "주석", "포맷", "포매팅", "줄바꿈", "공백",
    "간단히", "단순히", "typo", "rename", "comment", "format", "whitespace",
    "설명", "요약", "알려줘", "뭐야", "뭐니", "explain", "summar",
)

_FILE_MENTION_PATTERN = re.compile(r"[\w./-]+\.\w{1,5}\b")


def score_task_complexity(task: str, conversation_history: list[str] | None = None) -> int:
    """작업 설명을 보고 복잡도 점수(정수, 클수록 어려움)를 매긴다.

    LLM 호출 없이 순수 규칙 기반으로 판단해 지연/비용이 들지 않는다.
    절대값 자체보다 openrouter.models의 max_score 경계와 상대적으로만
    비교되므로, 정확한 스케일보다 "쉬움 < 보통 < 어려움" 순서가 맞는지가
    중요하다.
    """
    if not task:
        return 0
    t = task.strip()
    tl = t.lower()
    score = 0

    # 1) 길이: 길고 자세한 요청일수록 복잡한 요구사항일 가능성이 높다.
    length = len(t)
    if length > 400:
        score += 4
    elif length > 150:
        score += 2
    elif length > 60:
        score += 1

    # 2) 어려움/쉬움 키워드
    hard_hits = sum(1 for kw in _HARD_KEYWORDS if kw in tl)
    easy_hits = sum(1 for kw in _EASY_KEYWORDS if kw in tl)
    score += min(hard_hits, 3) * 2
    score -= min(easy_hits, 3)

    # 3) 언급된 파일 개수 — 여러 파일에 걸친 작업일수록 복잡.
    file_mentions = set(_FILE_MENTION_PATTERN.findall(t))
    if len(file_mentions) >= 3:
        score += 3
    elif len(file_mentions) == 2:
        score += 1

    # 4) 이어지는 대화(직전 맥락 참조)는 맥락 파악이 필요해 약간 가중.
    if conversation_history:
        score += 1

    return max(score, 0)


def pick_openrouter_model(cfg: dict, task: str, conversation_history: list[str] | None = None) -> tuple[str, int]:
    """openrouter.models 목록에서 작업 복잡도에 맞는 모델을 고른다.

    목록은 [{name, max_score}, ...] 형태이며 **싼/약한 모델부터 순서대로**
    적혀 있어야 한다. 점수가 어떤 항목의 max_score 이하면 그 모델을 쓰고,
    모든 항목을 초과하면 마지막(가장 비싼/강한) 모델로 폴백한다.
    목록이 비어 있으면 openrouter.model(고정 모델)을 그대로 반환한다.

    반환값: (모델명, 산정된 복잡도 점수)
    """
    or_cfg = cfg.get("openrouter", {})
    score = score_task_complexity(task, conversation_history)
    models = or_cfg.get("models") or []
    if not models:
        return or_cfg.get("model", ""), score

    for entry in models:
        name = entry.get("name")
        max_score = entry.get("max_score")
        if not name:
            continue
        if max_score is None or score <= max_score:
            return name, score
    # 모든 항목의 max_score를 초과 → 목록 마지막(가장 강한) 모델로 폴백
    last = models[-1]
    return last.get("name", or_cfg.get("model", "")), score
