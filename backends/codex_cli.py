"""Codex CLI 비대화형 호출 래퍼"""
import subprocess


def run_codex(task: str, working_dir: str = ".", timeout: int = 600, apply: bool = False) -> dict:
    """codex exec '<task>' 형태로 호출.

    apply=True면 --full-auto 로 워크스페이스 내 편집을 자동 승인해 실제 적용한다.
    설치된 codex 버전에 따라 옵션이 다를 수 있으니 'codex --help'로 확인하세요.
    """
    cmd = ["codex", "exec"]
    if apply:
        cmd += ["--full-auto"]
    cmd += [task]
    try:
        r = subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout)
        return {"success": r.returncode == 0, "output": r.stdout + r.stderr}
    except FileNotFoundError:
        return {"success": False, "output": "codex CLI를 찾을 수 없습니다. 설치 및 PATH 등록을 확인하세요."}
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "codex 실행 타임아웃"}
