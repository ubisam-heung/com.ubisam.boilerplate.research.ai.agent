"""작업 실행 지표 수집/집계 (경진대회 기대효과 정량화용)

매 작업 실행마다 logs/metrics.jsonl 한 줄을 남기고, summarize()로 발표용 수치를 만든다.
- 로컬 처리 비율(= 사내 코드 외부 미전송 비율)
- 평균 처리시간 / 평균 에이전틱 스텝 수
- 로컬 처리로 외부에 보내지 않은 추정 토큰 → 비용 절감 추정
"""
import json
import os
import re
from datetime import datetime

METRICS_FILE = "metrics.jsonl"

# 외부 LLM 100만 토큰당 추정 단가(USD). 실제 사용 모델 단가로 바꿔도 됨.
DEFAULT_PRICE_PER_MTOK = 3.0

# task 원문에 섞여 들어올 수 있는 민감정보(비밀번호/토큰/API 키/라이선스 키)를
# 키-값 패턴으로 탐지해 마스킹한다. gen3(workspace 프로젝트)의
# UbiCom.Net/Utility/Security/SensitiveLogRedactor.cs 마스킹 정책을 이식.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_ -]?key|license[_ -]?key)\b"
    r"(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)",
)
_REDACTED_VALUE = r"\1\2[REDACTED]"


def redact_sensitive(text: str) -> str:
    """password/token/api key/license key 등 키-값 쌍을 [REDACTED]로 마스킹한다.

    마스킹 자체가 실패해도 원문을 그대로 흘리지 않고 안전한 대체 문자열을 반환한다
    (fail-safe — SensitiveLogRedactor.cs와 동일한 원칙).
    """
    if not text:
        return text
    try:
        return _SENSITIVE_KEY_PATTERN.sub(_REDACTED_VALUE, text)
    except Exception:
        return "[REDACTION_FAILED]"


def record_run(log_dir: str, data: dict) -> str:
    """작업 실행 1건을 JSONL로 추가 기록한다. 실패해도 조용히 넘어간다."""
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, METRICS_FILE)
        if isinstance(data.get("task"), str):
            data = {**data, "task": redact_sensitive(data["task"])}
        row = {"ts": datetime.now().isoformat(timespec="seconds"), **data}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return ""


def load(log_dir: str) -> list:
    path = os.path.join(log_dir, METRICS_FILE)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _ratio(num: int, den: int):
    return (num / den) if den else None


# 백엔드 표시 순서·라벨. tool 필드는 표기가 제각각이라(claude/claude_code/codex) 정규화한다.
BACKENDS = ("local", "openrouter", "claude_code", "codex")
BACKEND_LABELS = {"local": "로컬 LLM", "openrouter": "OpenRouter", "claude_code": "Claude Code", "codex": "Codex"}
_TOOL_ALIASES = {"claude": "claude_code", "claudecode": "claude_code"}


def backend_of(record: dict) -> str:
    """작업 1건이 어떤 백엔드에서 처리됐는지 정규화해 반환한다.

    decision이 local/openrouter면 그대로, external이면 tool 필드
    (claude/claude_code/codex)를 표준 키로 정규화한다. 분류 불가(n/a 등)는 None.
    """
    decision = record.get("decision")
    if decision in ("local", "openrouter"):
        return decision
    if decision == "external":
        tool = (record.get("tool") or "").strip().lower()
        return _TOOL_ALIASES.get(tool, tool) or "claude_code"
    return None


def summarize(records: list, price_per_mtok: float = DEFAULT_PRICE_PER_MTOK) -> dict:
    total = len(records)
    durations = [r["duration_sec"] for r in records
                 if isinstance(r.get("duration_sec"), (int, float))]

    # 백엔드별 집계 (local / claude_code / codex 전부)
    by_backend = {}
    routed_total = 0
    for r in records:
        b = backend_of(r)
        if b is None:
            continue
        routed_total += 1
        agg = by_backend.setdefault(b, {"count": 0, "tokens": 0})
        agg["count"] += 1
        agg["tokens"] += int(r.get("est_tokens") or 0)

    backends = {}
    for key in BACKENDS:
        agg = by_backend.get(key, {"count": 0, "tokens": 0})
        backends[key] = {
            "label": BACKEND_LABELS[key],
            "count": agg["count"],
            "tokens": agg["tokens"],
            "ratio": _ratio(agg["count"], routed_total),
        }
    # BACKENDS에 없는 미지의 백엔드도 빠뜨리지 않고 포함
    for key, agg in by_backend.items():
        if key in backends:
            continue
        backends[key] = {
            "label": BACKEND_LABELS.get(key, key),
            "count": agg["count"],
            "tokens": agg["tokens"],
            "ratio": _ratio(agg["count"], routed_total),
        }

    # "로컬 처리 비율"은 사내 코드를 외부로 보내지 않은 비율 — openrouter는 외부 API로
    # 코드가 나가므로 local이 아니라 external 쪽으로 집계한다.
    local = by_backend.get("local", {"count": 0, "tokens": 0})
    external_count = routed_total - local["count"]
    local_tokens = local["tokens"]

    steps = [int(r["steps_taken"]) for r in records
              if isinstance(r.get("steps_taken"), (int, float))]

    # OpenRouter 프롬프트 캐싱 통계 (backends/openrouter.py의 cache_stats()가
    # agent.py를 통해 기록). 캐시 읽기는 정가의 약 10%로 청구되므로, 캐시가
    # 없었다면 그 토큰도 풀프라이스였을 것이라는 가정으로 절감액을 추정한다.
    cache_read = sum(int(r.get("cache_read_tokens") or 0) for r in records)
    cache_write = sum(int(r.get("cache_write_tokens") or 0) for r in records)
    cache_savings_usd = cache_read / 1_000_000 * price_per_mtok * 0.9

    # openrouter.auto_model 자동 모델 선택 통계 (router.pick_openrouter_model이
    # 산정한 complexity_score/selected_model — agent.py가 auto_model일 때만 기록).
    # 점수 자체는 router._HARD_KEYWORDS 등 임계값 튜닝 근거로, 모델별 건수는
    # 실제로 어떤 등급 모델이 얼마나 선택됐는지 확인하는 용도.
    scored = [r for r in records if r.get("complexity_score") is not None]
    scores = [int(r["complexity_score"]) for r in scored]
    by_model = {}
    for r in scored:
        m = r.get("selected_model") or "(알 수 없음)"
        by_model[m] = by_model.get(m, 0) + 1

    # 코드 변경 완료율: outcome이 completed/failed인 건만 대상으로 한다(explain/chatter/
    # interactive 등 코드 수정을 시도하지 않은 건은 분모에서 제외). "Task 성공률"을
    # 심사위원이 로그를 직접 열어보지 않아도 리포트에서 바로 확인할 수 있게 한다.
    outcome_counts: dict[str, int] = {}
    for r in records:
        oc = r.get("outcome")
        if oc:
            outcome_counts[oc] = outcome_counts.get(oc, 0) + 1
    completed = outcome_counts.get("completed", 0)
    failed = outcome_counts.get("failed", 0)
    attempted = completed + failed
    interactive = outcome_counts.get("interactive", 0)

    # 외부 CLI(claude/codex) 대화형 세션의 성공/실패는 자동 판정이 불가능해 세션 종료
    # 시점에 사람이 직접 태깅한다(cli.py의 _ask_session_outcome). session_outcome이
    # 없는(옛 로그·비대화형 실행) 레코드는 "unknown"으로 취급해 조용히 제외한다.
    session_success = sum(1 for r in records if r.get("session_outcome") == "success")
    session_failure = sum(1 for r in records if r.get("session_outcome") == "failure")
    session_tagged = session_success + session_failure

    return {
        "total": total,
        "routed_total": routed_total,
        "backends": backends,
        "local": local["count"],
        "external": external_count,
        # 로컬 비율은 라우팅된 작업(local+external) 기준 — no_files 등 비라우팅 건은 제외
        "local_ratio": _ratio(local["count"], routed_total),
        "avg_duration_sec": (sum(durations) / len(durations)) if durations else None,
        "avg_steps_taken": (sum(steps) / len(steps)) if steps else None,
        "local_tokens_kept_inhouse": local_tokens,
        "cost_avoided_usd": local_tokens / 1_000_000 * price_per_mtok,
        "price_per_mtok": price_per_mtok,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cache_savings_usd": cache_savings_usd,
        "auto_model_count": len(scored),
        "auto_model_avg_score": (sum(scores) / len(scores)) if scores else None,
        "auto_model_by_model": by_model,
        "completed": completed,
        "failed": failed,
        "attempted": attempted,
        "completion_rate": _ratio(completed, attempted),
        "interactive": interactive,
        "session_success": session_success,
        "session_failure": session_failure,
        "session_tagged": session_tagged,
        "session_success_rate": _ratio(session_success, session_tagged),
    }


def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def _num(x, suffix=""):
    return "—" if x is None else f"{x:.1f}{suffix}"


def format_report(summary: dict) -> str:
    s = summary
    if s["total"] == 0:
        return "수집된 작업 지표가 없습니다. 먼저 ./agent 로 작업을 몇 건 실행하세요."
    lines = [
        "AI Agent 작업 지표 (기대효과 정량화)",
        "─" * 44,
        f"  총 작업 건수            {s['total']}건",
        "  백엔드별 처리 분포",
    ]
    for key in BACKENDS:
        b = s["backends"].get(key)
        if not b:
            continue
        lines.append(
            f"    {b['label']:<12} {_pct(b['ratio'])}  ({b['count']}건)"
        )
    # 알려지지 않은 백엔드도 표시
    for key, b in s["backends"].items():
        if key in BACKENDS:
            continue
        lines.append(
            f"    {b['label']:<12} {_pct(b['ratio'])}  ({b['count']}건)"
        )
    lines += [
        f"  로컬 처리 비율          {_pct(s['local_ratio'])}  "
        f"(로컬 {s['local']} / 외부 {s['external']})",
        "  └ = 사내 코드 외부 미전송 비율",
        f"  평균 처리시간           {_num(s['avg_duration_sec'], '초')}",
        f"  평균 에이전틱 스텝 수    {_num(s['avg_steps_taken'])}",
        f"  외부 미전송 토큰(추정)  {s['local_tokens_kept_inhouse']:,} tok",
        f"  추정 비용 절감          ${s['cost_avoided_usd']:.2f}  "
        f"(@${s['price_per_mtok']}/1M tok)",
    ]
    if s.get("cache_read_tokens") or s.get("cache_write_tokens"):
        lines += [
            f"  OpenRouter 프롬프트 캐시 읽기  {s['cache_read_tokens']:,} tok",
            f"  OpenRouter 프롬프트 캐시 쓰기  {s['cache_write_tokens']:,} tok",
            f"  캐싱으로 절감(추정)          ${s['cache_savings_usd']:.2f}",
        ]
    if s.get("auto_model_count"):
        lines.append(f"  자동 모델 선택 건수      {s['auto_model_count']}건 (평균 복잡도 점수 {_num(s['auto_model_avg_score'])})")
        for model, count in sorted(s["auto_model_by_model"].items(), key=lambda kv: -kv[1]):
            lines.append(f"    └ {model:<28} {count}건")
    if s.get("attempted"):
        lines.append(
            f"  코드 변경 완료율        {_pct(s['completion_rate'])}  "
            f"(완료 {s['completed']} / 실패 {s['failed']})"
        )
    if s.get("interactive"):
        lines.append(f"  외부 CLI 대화형 세션    {s['interactive']}건")
        if s.get("session_tagged"):
            lines.append(
                f"    └ 세션 성공률          {_pct(s['session_success_rate'])}  "
                f"(성공 {s['session_success']} / 실패 {s['session_failure']}, "
                f"{s['session_tagged']}/{s['interactive']}건 태깅됨)"
            )
    lines.append("─" * 44)
    return "\n".join(lines)
