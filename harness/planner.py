"""작업 계획(plan) 수립"""
import os

from harness import project_guide

PLAN_PROMPT = """{guide}작업: {task}

관련 파일 내용:
{file_contents}

이 작업을 수행할 계획을 세워라.
각 변경 항목마다 file, action(modify|create), description을 명시해라.
verify_commands에는 변경 후 검증할 셸 명령어를 넣어라 (예: pytest, npm test, python -m pyflakes 등).
프로젝트에 적합한 검증 명령이 불확실하면 빈 배열로 둬라.

JSON 형식 (이 형식 그대로, 다른 텍스트 없이):
{{
  "changes": [
    {{"file": "path/to/file.py", "action": "modify", "description": "구체적인 변경 내용"}}
  ],
  "verify_commands": ["pytest tests/ -q"]
}}
"""


def make_plan(llm, task: str, file_contents: dict[str, str], guide: str = "") -> dict:
    contents_str = "\n\n".join(
        f"### {f}\n```\n{c if c else '(새 파일 - 아직 내용 없음)'}\n```"
        for f, c in file_contents.items()
    ) or "(관련 파일 없음)"

    prompt = PLAN_PROMPT.format(guide=project_guide.as_prelude(guide), task=task, file_contents=contents_str)
    plan = llm.generate(prompt, json_mode=True, num_predict=4096)

    if not isinstance(plan, dict) or "changes" not in plan:
        raise ValueError(f"계획 생성 실패: 잘못된 형식 -> {plan}")

    plan.setdefault("verify_commands", [])
    return plan


def _resolve_change_file(raw: str, known: list[str], by_base: dict[str, list[str]]) -> str | None:
    """계획의 file 값을 실제 대상 경로로 보정한다. 보정 불가면 None(폐기 대상).

    - 선택된 파일과 정확히 일치 → 그대로
    - 파일명(basename)이 선택된 파일 중 하나와 유일 매칭 → 그 경로로 보정
      (로컬 모델이 긴 경로를 '/contents/x.vue'처럼 줄여 뱉는 사고를 잡는다)
    - 작업 범위 밖(절대경로·.. 탈출·매칭 실패) → None
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    norm = raw.lstrip("./").replace("\\", "/")
    # work_root 밖으로 나가는 경로는 보정 시도 없이 폐기
    if raw.startswith("/") or norm.startswith("../") or "/../" in norm:
        base = os.path.basename(norm)
        hits = by_base.get(base, [])
        return hits[0] if len(hits) == 1 else None
    if norm in known:
        return norm
    base = os.path.basename(norm)
    hits = by_base.get(base, [])
    if len(hits) == 1:
        return hits[0]
    return None


def validate_changes(changes: list, known_files: list[str]) -> tuple[list, list]:
    """계획의 각 변경 file을 선택·검토된 파일 집합에 맞춰 보정/폐기한다.

    planner LLM이 선택되지 않은(읽지도 않은) 파일을 지어내거나 경로를 변조하는
    것을 막는다. 반환: (살릴 변경 리스트, [(원본경로, 폐기사유), ...]).
    """
    known = list(known_files or [])
    by_base: dict[str, list[str]] = {}
    for k in known:
        by_base.setdefault(os.path.basename(k), []).append(k)

    clean, dropped = [], []
    for ch in changes:
        raw = str(ch.get("file", ""))
        resolved = _resolve_change_file(raw, known, by_base)
        if resolved is None:
            dropped.append((raw, "선택된 파일과 매칭 불가(경로 변조/범위 밖 추정)"))
            continue
        clean.append({**ch, "file": resolved})
    return clean, dropped


def untouched_targets(task: str, known_files: list[str], changes: list) -> list[str]:
    """작업이 파일명으로 콕 집었는데 계획이 건드리지 않은 파일들을 돌려준다.

    예: 작업이 'limsAlarms2.vue에 …'인데 계획에 그 파일이 없으면 로컬이 탈선한 것.
    반환이 비어있지 않으면 외부 폴백 신호로 쓴다.
    """
    t = (task or "").lower()
    planned = {str(c.get("file", "")) for c in changes}
    targets = []
    for k in known_files or []:
        if os.path.basename(k).lower() in t and k not in planned:
            targets.append(k)
    return targets
