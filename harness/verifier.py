"""검증 명령(테스트/빌드/린트) 실행"""
import subprocess


def run_verification(commands: list[str], cwd: str = ".", timeout: int = 120) -> list[dict]:
    results = []
    for cmd in commands:
        if not cmd.strip():
            continue
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
            )
            results.append({
                "cmd": cmd,
                "success": r.returncode == 0,
                "returncode": r.returncode,
                "output": (r.stdout + "\n" + r.stderr).strip(),
            })
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "success": False, "returncode": -1, "output": "Timeout"})
        except Exception as e:
            results.append({"cmd": cmd, "success": False, "returncode": -1, "output": str(e)})
    return results


def all_passed(results: list[dict]) -> bool:
    return all(r["success"] for r in results)
