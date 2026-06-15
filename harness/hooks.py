"""`.agent-harness/hooks/` 스크립트 호출 래퍼"""
import os
import subprocess

HOOK_DIR = ".agent-harness/hooks"


class HookBlocked(Exception):
    """hook이 작업을 차단했을 때 발생"""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _as_roots(roots) -> list:
    """단일 경로(str) 또는 경로 목록을 받아 중복 없는 리스트로 정규화한다."""
    if isinstance(roots, str):
        roots = [roots]
    seen, out = set(), []
    for r in roots:
        key = os.path.normpath(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _run_hook(roots, hook_name: str, arg: str) -> list:
    """주어진 root들에서 존재하는 hook을 모두 실행한다.

    프레임워크 루트(공통 가드레일)와 프로젝트 루트(프로젝트별 hook)를 함께 넘기면,
    각 위치의 `.agent-harness/hooks/<hook_name>`를 순서대로 실행한다.
    반환: [(returncode, output), ...] — 실제로 존재해서 실행된 hook만.
    """
    results = []
    for root in _as_roots(roots):
        hook_path = os.path.join(root, HOOK_DIR, hook_name)
        if not os.path.exists(hook_path):
            continue  # hook 없으면 통과 (선택 설치 가능하게)
        r = subprocess.run(
            ["bash", hook_path, arg],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        results.append((r.returncode, (r.stdout + r.stderr).strip()))
    return results


def check_pre_bash(roots, command: str):
    """위험한 셸 명령이면 HookBlocked 발생. roots 중 하나라도 차단하면 차단."""
    for code, output in _run_hook(roots, "pre-bash.sh", command):
        if code != 0:
            raise HookBlocked(output or f"pre-bash hook이 명령을 차단했습니다: {command}")


def check_pre_file(roots, filepath: str):
    """민감 파일 접근이면 HookBlocked 발생. roots 중 하나라도 차단하면 차단."""
    for code, output in _run_hook(roots, "pre-file.sh", filepath):
        if code != 0:
            raise HookBlocked(output or f"pre-file hook이 파일 접근을 차단했습니다: {filepath}")


def run_post_edit(roots, filepath: str) -> str:
    """수정 후 검증. 실패해도 예외 없이 각 hook 출력을 합쳐 반환."""
    outputs = [out for _, out in _run_hook(roots, "post-edit.sh", filepath) if out]
    return "\n".join(outputs)
