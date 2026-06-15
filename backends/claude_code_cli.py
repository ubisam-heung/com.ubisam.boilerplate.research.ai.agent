"""Claude Code CLI 비대화형 호출 래퍼"""
import subprocess


def run_claude_code(task: str, working_dir: str = ".", timeout: int = 600, apply: bool = False) -> dict:
    """claude -p '<task>' 형태로 호출.

    apply=False: print 모드 — 파일을 고치지 않고 제안만 출력(미리보기).
    apply=True:  --permission-mode acceptEdits 로 파일 편집을 자동 승인해 실제 적용.
    설치된 claude code 버전에 따라 옵션이 다를 수 있으니 'claude --help'로 확인하세요.
    """
    cmd = ["claude", "-p"]
    if apply:
        cmd += ["--permission-mode", "acceptEdits"]
    cmd += [task]
    try:
        r = subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout)
        return {"success": r.returncode == 0, "output": r.stdout + r.stderr}
    except FileNotFoundError:
        return {"success": False, "output": "claude CLI를 찾을 수 없습니다. 설치 및 PATH 등록을 확인하세요."}
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "claude code 실행 타임아웃"}
