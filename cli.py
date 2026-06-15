#!/usr/bin/env python3
"""AI Agent TUI — 대화형 터미널 인터페이스

Usage:
    python cli.py              # 대화형 모드
    python cli.py "작업 설명"   # 단일 작업 실행
"""
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

try:
    import readline
    HAS_READLINE = True
except ImportError:
    HAS_READLINE = False

if HAS_READLINE:
    readline.set_history_length(500)
    _HISTORY = Path.home() / ".agent_history"
    # 히스토리는 부가 기능일 뿐이므로, 읽기 실패(권한 오류 등)가 앱을 죽이지 않게 한다.
    try:
        if _HISTORY.exists():
            readline.read_history_file(_HISTORY)
    except OSError:
        pass

import yaml
from harness import metrics
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

console = Console(highlight=False)

VERSION = "1.0.0"

# 선택 가능한 실행 모델. auto=자동 라우팅, 나머지는 해당 백엔드 강제.
MODELS = ("auto", "local", "claude", "codex")
_MODEL_ALIASES = {"claude_code": "claude", "claudecode": "claude"}

# 슬래시 없이도 인식할 명령어. (예: "exit" == "/exit")
BARE_COMMANDS = {"help", "model", "status", "config", "history", "clear", "metrics", "save", "exit", "quit", "q"}


class SessionRecorder:
    """CLI 대화 세션(입력 + 출력)을 기록하고 마크다운 파일로 저장한다."""

    def __init__(self, sessions_dir: str):
        self.sessions_dir = Path(sessions_dir)
        self.started_at = datetime.now()
        self.turns: List[dict] = []
        self._buf: List[str] = []
        self._cur_input: Optional[str] = None
        self._cur_model: str = "auto"

    def start_turn(self, user_input: str, model: str):
        self._cur_input = user_input
        self._cur_model = model
        self._buf = []

    def append(self, line: str):
        if self._cur_input is not None:
            self._buf.append(line)

    def end_turn(self, interactive: bool = False):
        if self._cur_input is None:
            return
        self.turns.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "model": self._cur_model,
            "input": self._cur_input,
            "output": ("(대화형 세션 — TTY 직접 연결, 출력 캡처 불가)"
                       if interactive else "\n".join(self._buf)),
            "interactive": interactive,
        })
        self._cur_input = None
        self._buf = []

    def save(self, tag: str = "") -> Optional[Path]:
        if not self.turns:
            return None
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        ts = self.started_at.strftime("%Y%m%d_%H%M%S")
        suffix = f"_{tag}" if tag else ""
        path = self.sessions_dir / f"{ts}{suffix}.md"

        lines = [
            f"# AI Agent 세션  {self.started_at.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        for i, t in enumerate(self.turns, 1):
            lines += [
                f"## [{i}] {t['ts']}  `{t['model']}`",
                "",
                "**입력**",
                "",
                f"> {t['input']}",
                "",
                "**출력**",
                "",
                "```",
                t["output"],
                "```",
                "",
            ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

_BANNER = """\
[bold blue] ╔══════════════════════════════════════╗[/bold blue]
[bold blue] ║[/bold blue]  [bold cyan]AI AGENT[/bold cyan]  [dim]v{v}[/dim]                       [bold blue]║[/bold blue]
[bold blue] ║[/bold blue]  [dim]로컬 AI 에이전트 프레임워크[/dim]          [bold blue]║[/bold blue]
[bold blue] ╚══════════════════════════════════════╝[/bold blue]
""".format(v=VERSION)

_HELP = """\
[bold]명령어[/bold]  [dim](슬래시 없이 입력해도 됩니다: exit == /exit)[/dim]

  [cyan]/help[/cyan]            이 도움말
  [cyan]/model[/cyan]           실행 모델 선택 (auto·local·claude·codex)
  [cyan]/status[/cyan]          연결 상태 확인
  [cyan]/config[/cyan]          현재 설정 보기
  [cyan]/metrics[/cyan]         작업 지표 요약 (로컬처리율·복구율 등)
  [cyan]/history[/cyan]         최근 작업 목록
  [cyan]/save [태그][/cyan]     현재 세션을 파일로 저장 (선택: 이름 태그)
  [cyan]/clear[/cyan]           화면 지우기
  [cyan]/exit[/cyan]            종료 (세션 자동 저장)

[bold]작업 입력[/bold]

  프롬프트에 자연어로 작업을 입력하세요.
  예)  [italic]main.py에 에러 처리 추가[/italic]
  예)  [italic]테스트 파일 없는 함수에 pytest 추가[/italic]
"""


# ─── 로그 파서 ────────────────────────────────────────────────────────────────

_STEP_RE = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)")
_OK_RE = re.compile(r"\[완료\]|\bOK\b")
_FAIL_RE = re.compile(r"\[실패\]|\[오류\]|\bFAIL\b")
_BLOCKED_RE = re.compile(r"\[차단됨\]")
_CMD_RE = re.compile(r"^\s+\$\s")
_DIFF_RE = re.compile(r"^(---|\+\+\+|@@|[-+] )")


def _rich_confirm(question: str) -> bool:
    """외부 도구 변경 적용 여부를 Rich 프롬프트로 묻는다."""
    try:
        ans = console.input(f"\n  [bold yellow]?[/bold yellow]  {escape(question)} [dim]\\[y/N][/dim] ")
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False
    return ans.strip().lower() in ("y", "yes")


def _rich_log(message: str):
    """에이전트 print 출력을 Rich 스타일로 변환."""
    msg = message.rstrip()

    if not msg:
        return

    m = _STEP_RE.match(msg)
    if m:
        num, total, text = m.group(1), m.group(2), m.group(3)
        console.print(
            f"  [dim]{num}/{total}[/dim]  [bold]{escape(text)}[/bold]"
        )
        return

    if _OK_RE.search(msg):
        console.print(f"  [bold green]✓[/bold green]  {escape(msg)}")
        return

    if _FAIL_RE.search(msg):
        console.print(f"  [bold red]✗[/bold red]  {escape(msg)}")
        return

    if _BLOCKED_RE.search(msg):
        console.print(f"  [bold yellow]⚠[/bold yellow]  {escape(msg)}")
        return

    if msg.startswith("[post-edit]"):
        console.print(f"  [dim cyan]↳ {escape(msg[11:].strip())}[/dim cyan]")
        return

    if _CMD_RE.match(msg):
        console.print(f"  [dim]{escape(msg.strip())}[/dim]")
        return

    if _DIFF_RE.match(msg):
        if msg.startswith("+") and not msg.startswith("+++"):
            console.print(f"[green]{escape(msg)}[/green]")
        elif msg.startswith("-") and not msg.startswith("---"):
            console.print(f"[red]{escape(msg)}[/red]")
        else:
            console.print(f"[dim]{escape(msg)}[/dim]")
        return

    if msg.startswith("  - ") or msg.startswith("    "):
        console.print(f"  [dim]{escape(msg.strip())}[/dim]")
        return

    console.print(f"  {escape(msg)}")


# ─── 상태 확인 ────────────────────────────────────────────────────────────────

def _load_config(project_dir: str = ".") -> dict:
    p = Path(project_dir) / "config.yaml"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _check_status(config: dict, project_dir: str) -> dict:
    status = {
        "ollama": False,
        "model": "—",
        "project_dir": project_dir,
    }
    try:
        import requests
        llm = config.get("local_llm", {})
        base = llm.get("base_url", "http://192.168.0.229:11345")
        r = requests.get(f"{base}/api/tags", timeout=3)
        if r.ok:
            status["ollama"] = True
            names = [m["name"] for m in r.json().get("models", [])]
            target = llm.get("model", "")
            if target in names:
                status["model"] = target
            else:
                prefix = target.split(":")[0]
                status["model"] = next((n for n in names if n.startswith(prefix)), target)
    except Exception:
        pass
    return status


def _print_status(status: dict):
    t = Table(box=None, show_header=False, padding=(0, 1))
    t.add_column("dot", width=3)
    t.add_column("label", style="bold", width=14)
    t.add_column("value")

    dot_ok = "[green]●[/green]"
    dot_err = "[red]●[/red]"
    dot_dim = "[dim]●[/dim]"

    if status["ollama"]:
        t.add_row(dot_ok, "Ollama", "[green]연결됨[/green]")
        t.add_row(dot_ok, "모델", f"[cyan]{escape(status['model'])}[/cyan]")
    else:
        t.add_row(dot_err, "Ollama", "[red]연결 안 됨[/red]  →  ollama serve")

    t.add_row(dot_dim, "프로젝트", f"[dim]{escape(status['project_dir'])}[/dim]")
    console.print(t)


# ─── 대화형 루프 ─────────────────────────────────────────────────────────────

def run_interactive(project_dir: str = "."):
    console.print(_BANNER)

    config = _load_config(project_dir)
    with console.status("[dim]시스템 확인 중...[/dim]", spinner="dots"):
        status = _check_status(config, project_dir)

    _print_status(status)

    if not status["ollama"]:
        console.print(
            "\n  [yellow]⚠[/yellow]  Ollama가 실행되지 않았습니다. "
            "[dim]ollama serve[/dim] 를 먼저 실행하세요.\n"
        )
    else:
        console.print()

    console.print("  [dim]/help 명령어 목록  ·  /model 모델 선택  ·  /save 저장  ·  /exit 종료[/dim]\n")

    sessions_dir = os.path.join(project_dir, config.get("sessions", {}).get("dir", "sessions"))
    recorder = SessionRecorder(sessions_dir)

    history: List[str] = []
    ui = {"model": "auto"}

    while True:
        try:
            prompt = (
                f"[bold blue]agent[/bold blue] "
                f"[dim]({ui['model']})[/dim] [bold]❯[/bold] "
            )
            task = console.input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n  [dim]종료합니다.[/dim]")
            break

        if not task:
            continue

        # 슬래시(/exit) 뿐 아니라 맨 명령어(exit, model claude 등)도 명령으로 처리한다.
        low = task.lower()
        first = low.split()[0]
        is_command = task.startswith("/") or low in BARE_COMMANDS or first == "model"
        if is_command:
            norm = task if task.startswith("/") else "/" + task
            _handle_command(norm, status, config, history, project_dir, ui, recorder=recorder)
            if norm.lower().split()[0] in ("/exit", "/quit", "/q"):
                break
            continue

        history.append(task)
        recorder.start_turn(task, ui["model"])

        if ui["model"] in ("claude", "codex"):
            # 명시적 외부 모델: 대화형 세션으로 전환 후 즉시 저장
            _run_external_interactive(ui["model"], task, _work_root(project_dir, config), config)
            recorder.end_turn(interactive=True)
            _autosave(recorder)
        elif ui["model"] == "local":
            # 로컬 강제: 기존 파이프라인 실행
            def _rec_log(msg: str, _r=recorder):
                _r.append(msg)
                _rich_log(msg)
            _run_task(task, project_dir, "local", log_fn=_rec_log)
            recorder.end_turn()
        else:
            # auto: 잡담은 로컬, 코딩은 외부 대화형 세션으로 직행
            _run_auto_mode(task, project_dir, config, recorder)

    # 세션 종료 시 자동 저장
    saved = recorder.save()
    if saved:
        console.print(f"  [dim]세션 자동 저장됨: {escape(str(saved))}[/dim]")

    if HAS_READLINE:
        try:
            readline.write_history_file(_HISTORY)
        except OSError:
            pass


def _handle_command(
    raw: str,
    status: dict,
    config: dict,
    history: List[str],
    project_dir: str,
    ui: dict,
    recorder: Optional["SessionRecorder"] = None,
):
    cmd = raw.lower().split()[0]

    if cmd in ("/exit", "/quit", "/q"):
        console.print("  [dim]종료합니다.[/dim]")
        return

    if cmd == "/model":
        parts = raw.split()
        if len(parts) == 1:
            console.print(f"  현재 모델: [cyan]{ui['model']}[/cyan]")
            console.print(
                f"  [dim]사용 가능: {', '.join(MODELS)}"
                f"  (예: /model claude)[/dim]"
            )
        else:
            choice = parts[1].lower()
            choice = _MODEL_ALIASES.get(choice, choice)
            if choice not in MODELS:
                console.print(
                    f"  [yellow]알 수 없는 모델:[/yellow] {escape(parts[1])}"
                    f"  [dim]— {', '.join(MODELS)}[/dim]"
                )
            else:
                ui["model"] = choice
                console.print(
                    f"  [green]✓[/green]  실행 모델을 [cyan]{choice}[/cyan] 로 설정했습니다."
                )
        return

    if cmd == "/help":
        console.print(
            Panel(_HELP, title="[bold]도움말[/bold]", border_style="dim", box=box.ROUNDED)
        )

    elif cmd == "/clear":
        console.clear()
        console.print(_BANNER)

    elif cmd == "/status":
        cfg = _load_config(project_dir)
        with console.status("[dim]확인 중...[/dim]", spinner="dots"):
            st = _check_status(cfg, project_dir)
        _print_status(st)

    elif cmd == "/config":
        cfg = _load_config(project_dir)
        console.print(
            Panel(
                escape(yaml.dump(cfg, allow_unicode=True, default_flow_style=False)),
                title="[bold]config.yaml[/bold]",
                border_style="dim",
                box=box.ROUNDED,
            )
        )

    elif cmd == "/metrics":
        log_dir = os.path.join(project_dir, config.get("logging", {}).get("log_dir", "logs"))
        report = metrics.format_report(metrics.summarize(metrics.load(log_dir)))
        console.print(
            Panel(escape(report), title="[bold]작업 지표[/bold]",
                  border_style="dim", box=box.ROUNDED)
        )

    elif cmd == "/history":
        if not history:
            console.print("  [dim]기록이 없습니다.[/dim]")
        else:
            for i, h in enumerate(history[-20:], 1):
                console.print(f"  [dim]{i:2}.[/dim]  {escape(h)}")

    elif cmd == "/save":
        if recorder is None:
            console.print("  [yellow]세션 레코더가 초기화되지 않았습니다.[/yellow]")
            return
        parts = raw.split(maxsplit=1)
        tag = parts[1].strip() if len(parts) > 1 else ""
        saved = recorder.save(tag=tag)
        if saved:
            console.print(f"  [green]✓[/green]  세션 저장됨: [cyan]{escape(str(saved))}[/cyan]")
        else:
            console.print("  [dim]저장할 대화 기록이 없습니다.[/dim]")

    else:
        console.print(f"  [yellow]알 수 없는 명령어:[/yellow] {escape(raw)}  [dim]— /help 참고[/dim]")


def _work_root(project_dir: str, config: dict) -> str:
    """config의 harness.work_dir를 적용한 실제 작업 폴더."""
    work_dir = config.get("harness", {}).get("work_dir", ".")
    return os.path.normpath(os.path.join(project_dir, work_dir))


def _run_external_interactive(model: str, task: str, work_root: str, config: dict):
    """진짜 claude/codex 대화형 세션으로 현재 터미널을 넘긴다(핸드오프).

    출력을 캡처하지 않고 TTY를 그대로 물려줘서 권한 승인·대화 연속성이 네이티브로 동작한다.
    세션을 종료하면(exit / Ctrl-D) agent 루프로 복귀한다.
    """
    tool_key = "codex" if model == "codex" else "claude_code"
    ext = config.get("external_tools", {}).get(tool_key, {})
    base = ext.get("interactive_command") or ([model] if model == "codex" else ["claude"])
    cmd = list(base)
    if model_override := ext.get("model"):
        cmd += ["--model", model_override]
    cmd += [task] if task else []

    os.makedirs(work_root, exist_ok=True)
    console.print()
    console.print(Rule(f"[dim]{model} 대화형 세션[/dim]", style="dim blue"))
    console.print(
        f"  [dim]진짜 {model} 세션으로 전환합니다. "
        f"끝내려면 {model}에서 종료(exit · Ctrl-D)하면 agent로 돌아옵니다.[/dim]"
    )
    console.print(f"  [dim]작업 폴더: {escape(work_root)}[/dim]\n")

    t0 = time.monotonic()
    try:
        subprocess.run(cmd, cwd=work_root)
    except FileNotFoundError:
        console.print(
            f"  [bold red]오류:[/bold red]  {model} CLI를 찾을 수 없습니다. "
            f"설치 및 PATH 등록을 확인하세요."
        )
    except KeyboardInterrupt:
        pass

    log_dir = config.get("logging", {}).get("log_dir", "logs")
    metrics.record_run(log_dir, {
        "task": task[:200], "decision": "external", "outcome": "interactive",
        "tool": model, "duration_sec": round(time.monotonic() - t0, 2),
    })

    console.print()
    console.print(Rule(f"[dim]{model} 세션 종료 — agent로 복귀[/dim]", style="dim"))
    console.print()


def _run_task(task: str, project_dir: str, model: str = "auto", log_fn=None):
    label = task if len(task) <= 55 else task[:52] + "..."
    console.print()
    console.print(Rule(f"[dim]{escape(label)}[/dim]", style="dim blue"))

    force = None if model == "auto" else model
    effective_log = log_fn if log_fn is not None else _rich_log
    start = time.monotonic()
    try:
        from agent import run_agent
        run_agent(task, root=project_dir, log_fn=effective_log, force=force, confirm_fn=_rich_confirm)
    except KeyboardInterrupt:
        console.print("\n  [yellow]⚠[/yellow]  작업이 중단되었습니다.")
    except Exception as exc:
        console.print(f"\n  [bold red]오류:[/bold red]  {escape(str(exc))}")

    elapsed = time.monotonic() - start
    console.print()
    console.print(Rule(f"[dim]완료  ({elapsed:.1f}초)[/dim]", style="dim"))
    console.print()


def _autosave(recorder: SessionRecorder):
    """현재 세션을 즉시 파일로 저장하고 경로를 출력한다."""
    saved = recorder.save()
    if saved:
        console.print(f"  [dim]저장됨 → {escape(str(saved))}[/dim]")


# 파일 확장자나 명백한 개발 키워드가 있으면 LLM 판단 없이 즉시 외부로
_CODING_SIGNALS = (
    ".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".cpp", ".c", ".h",
    ".rb", ".php", ".kt", ".swift", ".cs", ".vue", ".html", ".css", ".sql", ".sh",
    "테스트", "함수", "클래스", "메서드", "코드", "버그", "수정", "추가", "구현",
    "리팩토", "빌드", "컴파일", "디버그", "api", "endpoint", "import", "class ",
    "def ", "return ", "test", "spec", "fix", "implement", "refactor",
)


def _is_obviously_coding(task: str) -> bool:
    tl = task.lower()
    return any(sig in task or sig in tl for sig in _CODING_SIGNALS)


def _run_auto_mode(task: str, project_dir: str, config: dict, recorder: SessionRecorder):
    """auto 모드: 명백한 코딩은 즉시 외부 위임, 애매하면 LLM 1회로 잡담/코딩 판단."""
    ext_default = config.get("external_tools", {}).get("default", "claude_code")
    model = "claude" if ext_default == "claude_code" else ext_default
    work_root = _work_root(project_dir, config)

    # 명백한 코딩 신호가 있으면 LLM 호출 없이 즉시 외부로
    if _is_obviously_coding(task):
        _run_external_interactive(model, task, work_root, config)
        recorder.end_turn(interactive=True)
        _autosave(recorder)
        return

    # 애매한 경우: LLM 1회로 잡담 여부만 판단
    from backends.local_llm import LocalLLM
    from router import is_chatter, reply_chatter

    llm_cfg = config.get("local_llm", {})
    llm = LocalLLM(
        model=llm_cfg.get("model", ""),
        base_url=llm_cfg.get("base_url", ""),
        temperature=0.0,
    )

    try:
        ollama_ok = llm.health_check()
    except Exception:
        ollama_ok = False

    if ollama_ok and is_chatter(llm, task):
        reply = reply_chatter(llm, task)
        recorder.append(reply)
        _rich_log(reply)
        recorder.end_turn()
        return

    # 잡담 아닌 것으로 판단(또는 Ollama 없음) → 외부 위임
    _run_external_interactive(model, task, work_root, config)
    recorder.end_turn(interactive=True)
    _autosave(recorder)


# ─── 단일 작업 모드 ───────────────────────────────────────────────────────────

def run_single(task: str, project_dir: str = "."):
    console.print(
        Panel(
            f"[bold]{escape(task)}[/bold]",
            title="[dim]작업[/dim]",
            border_style="dim",
            box=box.ROUNDED,
        )
    )
    console.print()
    try:
        from agent import run_agent
        run_agent(task, root=project_dir, log_fn=_rich_log, confirm_fn=_rich_confirm)
    except KeyboardInterrupt:
        console.print("\n  [yellow]⚠[/yellow]  작업이 중단되었습니다.")
        sys.exit(1)
    except Exception as exc:
        console.print(f"\n  [bold red]오류:[/bold red]  {escape(str(exc))}")
        sys.exit(1)


# ─── 진입점 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    project_dir = os.getcwd()
    if len(sys.argv) > 1:
        run_single(" ".join(sys.argv[1:]), project_dir)
    else:
        run_interactive(project_dir)
