"""작업 계획(plan) 수립"""
import os

from harness import project_guide

PLAN_PROMPT = """{guide}작업: {task}

관련 파일 내용:
{file_contents}

이 작업을 수행할 계획을 세워라.

작업이 파일 수정을 요구하면: 각 변경 항목마다 file, action(modify|create), description을 명시해라.
작업이 빌드/테스트/실행 등 셸 명령 실행만으로 끝나고 파일 수정이 필요 없다면:
changes를 빈 배열로 두고, verify_commands에 실행할 명령을 반드시 채워라
(예: "dotnet build UbiSam.Sources.Revision.sln", "pytest tests/ -q", "npm test").
changes와 verify_commands가 둘 다 비면 안 된다 — 최소 하나는 채워야 한다.

verify_commands의 각 항목은 문자열이거나 {{"cmd": "...", "expect_failure": true}} 객체다.
보통은 명령이 exit 0(성공)이어야 "통과"다 — 이때는 문자열만 써라.
단, "이 코드/텍스트가 없어야 한다", "아직 미구현이어야 한다"처럼 검색이 매칭되지
않는 것 자체가 정답인 검증(예: grep으로 특정 필드가 없는지 확인)은 grep이 매칭 없을 때
exit 1을 반환하는 게 정상이므로, 그 명령에는 반드시 "expect_failure": true를 붙여라.
안 붙이면 정상적으로 미구현임을 확인한 것도 "검증 실패"로 잘못 보고된다.

grep으로 만드는 검증 명령은 반드시 위 "관련 파일 내용"에 실제로 보이는 코드를
근거로만 만들어라. 파일 내용에 없는 변수명/패턴을 추측해서 넣지 마라 — 실제로는
있는데 이름을 잘못 짚으면 정상 구현도 "실패"로 오판된다.
`grep -A n ... | grep -c ...`처럼 파이프로 조합한 grep은 특히 신뢰도가 낮다
(첫 grep이 원하는 줄을 못 걸치면 두 번째 grep은 자동으로 매칭 0이 되는데, 이게
"lock이 없다"는 뜻인지 "애초에 첫 grep 범위 설정이 틀렸다"는 뜻인지 구분이
안 된다). 이런 명령은 되도록 피하고, 대신 파일 내용에서 이미 확인한 사실을
"grep -n"으로 그 줄 자체를 직접 짚는 단순한 명령으로 바꿔라. 정말 파이프가
필요하면 결과가 불확실할 수 있다는 뜻이니 expect_failure를 신중히 판단해라.

JSON 형식 (이 형식 그대로, 다른 텍스트 없이):
{{
  "changes": [
    {{"file": "path/to/file.py", "action": "modify", "description": "구체적인 변경 내용"}}
  ],
  "verify_commands": [
    "pytest tests/ -q",
    {{"cmd": "grep -n \\"Required\\" MessageItemDefinition.cs", "expect_failure": true}}
  ]
}}
"""


def make_plan(llm, task: str, file_contents: dict[str, str], guide: str = "") -> dict:
    contents_str = "\n\n".join(
        f"### {f}\n```\n{c if c else '(새 파일 - 아직 내용 없음)'}\n```"
        for f, c in file_contents.items()
    ) or "(관련 파일 없음)"

    prompt = PLAN_PROMPT.format(guide=project_guide.as_prelude(guide), task=task, file_contents=contents_str)
    # verify_commands가 {"cmd": ..., "expect_failure": true} 객체를 포함할 수 있게
    # 되면서 계획 JSON이 길어졌다. 4096으로는 grep 명령이 여러 개면 중간에 잘려
    # JSON 파싱이 깨지는 사례가 있어 8192로 올렸다.
    plan = llm.generate(prompt, json_mode=True, num_predict=8192)

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
