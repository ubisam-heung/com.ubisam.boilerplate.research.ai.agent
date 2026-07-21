"""검증 명령(테스트/빌드/린트) 실행"""
import os
import subprocess

# run_command 실행 시 자식 프로세스에 물려줄 환경변수 화이트리스트(sandbox_env=True일 때).
# 자격증명류(AWS_*, *_TOKEN, *_KEY, SSH_AUTH_SOCK 등)를 상속하지 않게 해, 에이전트가
# 실행하는 명령이 사용자 셸의 시크릿에 접근하는 경로를 줄인다. PATH 등 실행에 필요한
# 최소 항목만 남긴다.
_SANDBOX_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "PYTHONPATH")


def normalize_command(command) -> tuple[str, bool]:
    """검증 명령 항목을 (cmd, expect_failure)로 정규화한다.

    'grep으로 특정 문자열이 없어야 한다'류 음성 검증은 grep이 매칭 없을 때
    exit 1을 반환하는 게 정상이라, 이 경우 exit != 0을 "통과"로 봐야 한다.
    문자열만 오면(기존 방식) expect_failure=False로 취급해 하위 호환한다.
    """
    if isinstance(command, dict):
        return str(command.get("cmd", "")), bool(command.get("expect_failure", False))
    return str(command), False


def _sandboxed_env() -> dict:
    """화이트리스트에 있는 환경변수만 남긴 최소 환경을 만든다.

    자격증명(*_TOKEN, *_KEY, AWS_*, .netrc 관련 등)이 run_command로 실행되는
    셸 명령에 그대로 상속되지 않도록 하는 가벼운 격리. 컨테이너/네임스페이스
    수준의 격리는 아니며, "불필요한 환경변수 노출을 막는" 보완적 조치다.
    """
    return {k: v for k, v in os.environ.items() if k in _SANDBOX_ENV_ALLOWLIST}


def run_verification(commands: list, cwd: str = ".", timeout: int = 120,
                      sandbox_env: bool = False) -> list[dict]:
    """검증 명령들을 순서대로 실행한다.

    sandbox_env=True면 자식 프로세스에 전체 환경변수 대신 최소 화이트리스트만
    전달한다(harness.sandbox_env로 config.yaml에서 조정, 기본 False — 기존
    동작과 호환). 대상 프로젝트의 빌드가 특정 환경변수(예: .NET SDK 경로)에
    의존한다면 그 변수를 _SANDBOX_ENV_ALLOWLIST에 추가해야 한다.
    """
    env = _sandboxed_env() if sandbox_env else None
    results = []
    for raw in commands:
        cmd, expect_failure = normalize_command(raw)
        if not cmd.strip():
            continue
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                env=env,
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
