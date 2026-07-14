"""num_predict 상한에 걸려 중간에 잘린 JSON 응답을 최대한 복구하는 안전망.

주 해결책은 호출부에서 충분히 큰 num_predict를 주는 것이다(harness/planner.py
등 참고). 이 모듈은 그래도 잘렸을 때 완전히 실패하는 대신, 마지막으로 유효했던
지점까지만 잘라내 파싱 가능한 JSON으로 복구를 시도하는 최후의 보조 수단이다.
복구할 수 없으면 None을 반환한다(호출부가 원래 예외를 던지도록).
"""
import json

_MAX_CANDIDATES = 200


def _candidate_cut_points(text: str):
    """문자열 리터럴 밖에서, 값 하나가 완전히 끝난 직후의 콤마 위치를 뒤에서부터 낸다.

    "값이 끝난 직후"만 후보로 삼아야 마지막에 반쯤 쓰다 만 요소({"cmd": "grep...
    처럼)가 빈 {}로 둔갑해 섞여 들어가는 것을 막는다. 값의 끝은 닫는 따옴표
    직후, 숫자/true/false/null 리터럴 직후, 또는 }/] 직후다.
    """
    in_string = False
    escape = False
    positions = []
    value_ended_here = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                value_ended_here = True
            continue
        if ch == '"':
            in_string = True
            value_ended_here = False
        elif ch in "}]":
            value_ended_here = True
        elif ch == ",":
            if value_ended_here:
                positions.append(i)
            value_ended_here = False
        elif ch in "{[":
            value_ended_here = False
        elif ch.isspace():
            continue
        else:
            # 숫자/true/false/null 등 리터럴 문자 — 다음 콤마가 값 종료 직후일 수 있음
            value_ended_here = True
    return list(reversed(positions))


def try_repair_truncated_json(text: str):
    """잘린 JSON 텍스트에서 파싱 가능한 최대 prefix를 찾아 파싱 결과를 반환한다.

    뒤에서부터 후보 절단 지점을 시도하며, 열려 있는 배열/객체를 닫는 괄호를
    붙여 실제로 json.loads가 성공하는 첫 지점을 채택한다. 못 찾으면 None.
    """
    text = text.strip()
    if not text or text[0] not in "{[":
        return None

    for cut in _candidate_cut_points(text)[:_MAX_CANDIDATES]:
        prefix = text[:cut].rstrip().rstrip(",")
        if not prefix:
            continue
        stack = []
        in_string = False
        escape = False
        valid = True
        for ch in prefix:
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if not stack:
                    valid = False
                    break
                stack.pop()
        if not valid or in_string or not stack:
            continue
        closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
        candidate = prefix + closers
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None
