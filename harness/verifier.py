"""검증 명령(테스트/빌드/린트) 실행"""
import subprocess


def normalize_command(command) -> tuple[str, bool]:
    """검증 명령 항목을 (cmd, expect_failure)로 정규화한다.

    'grep으로 특정 문자열이 없어야 한다'류 음성 검증은 grep이 매칭 없을 때
    exit 1을 반환하는 게 정상이라, 이 경우 exit != 0을 "통과"로 봐야 한다.
    문자열만 오면(기존 방식) expect_failure=False로 취급해 하위 호환한다.
    """
    if isinstance(command, dict):
        return str(command.get("cmd", "")), bool(command.get("expect_failure", False))
    return str(command), False


def run_verification(commands: list, cwd: str = ".", timeout: int = 120) -> list[dict]:
    results = []
    for raw in commands:
        cmd, expect_failure = normalize_command(raw)
        if not cmd.strip():
            continue
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
            )
            passed = (r.returncode != 0) if expect_failure else (r.returncode == 0)
            results.append({
                "cmd": cmd,
                "success": passed,
                "returncode": r.returncode,
                "expect_failure": expect_failure,
                "output": (r.stdout + "\n" + r.stderr).strip(),
            })
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "success": False, "returncode": -1,
                             "expect_failure": expect_failure, "output": "Timeout"})
        except Exception as e:
            results.append({"cmd": cmd, "success": False, "returncode": -1,
                             "expect_failure": expect_failure, "output": str(e)})
    return results


def all_passed(results: list[dict]) -> bool:
    return all(r["success"] for r in results)
