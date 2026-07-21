#!/usr/bin/env bash
# AI Agent 설치 스크립트
# Usage: ./install.sh --target <디렉토리> [--tool all|claude|local|codex]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INVOKE_DIR="$PWD"   # 명령어를 입력한 터미널의 현재 위치 (상대경로 --target의 기준점)
TARGET=""
TOOL="all"

# ── 인수 파싱 ─────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target|-t) TARGET="$2"; shift 2 ;;
        --tool)      TOOL="$2";   shift 2 ;;
        -h|--help)
            echo "Usage: $0 --target <dir> [--tool all|claude|local|codex]"
            exit 0
            ;;
        *)
            echo "알 수 없는 옵션: $1"
            echo "Usage: $0 --target <dir> [--tool all|claude|local|codex]"
            exit 1
            ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    echo "오류: --target 옵션이 필요합니다."
    echo "Usage: $0 --target <dir> [--tool all|claude|local|codex]"
    exit 1
fi

# ~ 확장
TARGET="${TARGET/#\~/$HOME}"
# 절대경로가 아니면 명령어를 입력한 터미널 위치의 한 단계 위(부모) 기준으로 해석한다.
if [[ "$TARGET" != /* ]]; then
    TARGET="$(dirname "$INVOKE_DIR")/$TARGET"
fi
TARGET="$(mkdir -p "$TARGET" && cd "$TARGET" && pwd)"

# ── 유틸 ──────────────────────────────────────────────────────────────────────
_ok()   { printf "  \033[32m✓\033[0m  %s\n" "$1"; }
_fail() { printf "  \033[31m✗\033[0m  %s\n" "$1" >&2; }
_step() { printf "  \033[34m▸\033[0m  %s\n" "$1"; }

echo ""
echo "  ╔════════════════════════════════════════╗"
echo "  ║  AI Agent 설치                         ║"
printf "  ║  대상: %-33s║\n" "$TARGET"
printf "  ║  도구: %-33s║\n" "$TOOL"
echo "  ╚════════════════════════════════════════╝"
echo ""

# ── Python 확인 ───────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    _fail "python3를 찾을 수 없습니다. https://python.org 에서 설치하세요."
    exit 1
fi

PV=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PM=$(echo "$PV" | cut -d. -f1)
Pm=$(echo "$PV" | cut -d. -f2)

if [[ $PM -lt 3 ]] || [[ $PM -eq 3 && $Pm -lt 10 ]]; then
    _fail "Python 3.10 이상이 필요합니다. (현재: $PV)"
    exit 1
fi
_ok "Python $PV"

# ── 파일 복사 ─────────────────────────────────────────────────────────────────
_step "프레임워크 파일 복사 중..."

FILES=(agent.py cli.py router.py metrics.py config.yaml requirements.txt AGENTS.md doctor.sh)
DIRS=(backends harness src .agent-harness docs)

for f in "${FILES[@]}"; do
    [[ -f "$SCRIPT_DIR/$f" ]] && cp "$SCRIPT_DIR/$f" "$TARGET/$f"
done

for d in "${DIRS[@]}"; do
    if [[ -d "$SCRIPT_DIR/$d" ]]; then
        rm -rf "$TARGET/$d"
        cp -r "$SCRIPT_DIR/$d" "$TARGET/$d"
    fi
done

# __pycache__ 정리
find "$TARGET" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

mkdir -p "$TARGET/memory"
touch "$TARGET/memory/.gitkeep"

# 수정 대상 프로젝트가 들어갈 작업 폴더 (config.yaml의 harness.work_dir와 일치)
mkdir -p "$TARGET/workspace"
touch "$TARGET/workspace/.gitkeep"

_ok "파일 복사 완료"

# ── .env / .gitignore ─────────────────────────────────────────────────────────
[[ -f "$TARGET/.env" ]] || touch "$TARGET/.env"

cat > "$TARGET/.gitignore" <<'EOF'
.venv/
__pycache__/
*.pyc
.env
.agent_backup/
logs/
memory/*.md
!memory/.gitkeep
EOF
_ok ".gitignore"

# ── 가상환경 ──────────────────────────────────────────────────────────────────
_step "가상환경 생성 중..."
python3 -m venv "$TARGET/.venv"
_ok "가상환경 (.venv)"

# ── 의존성 설치 ───────────────────────────────────────────────────────────────
_step "패키지 설치 중..."
"$TARGET/.venv/bin/pip" install --quiet --upgrade pip
"$TARGET/.venv/bin/pip" install --quiet -r "$TARGET/requirements.txt"
_ok "패키지 설치 완료"

# ── 도구별 config 조정 ────────────────────────────────────────────────────────
if [[ "$TOOL" != "all" ]]; then
    python3 - "$TARGET/config.yaml" "$TOOL" <<'PYEOF'
import sys, yaml

config_path, tool = sys.argv[1], sys.argv[2]
tool_map = {"claude": "claude_code", "codex": "codex", "local": "claude_code"}
default = tool_map.get(tool, "claude_code")

with open(config_path) as f:
    cfg = yaml.safe_load(f)

cfg.setdefault("external_tools", {})["default"] = default
with open(config_path, "w") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
PYEOF
fi
_ok "설정 완료 (도구: $TOOL)"

# ── agent 실행 파일 ───────────────────────────────────────────────────────────
cat > "$TARGET/agent" <<AGENTEOF
#!/usr/bin/env bash
AGENT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
exec "\$AGENT_DIR/.venv/bin/python" "\$AGENT_DIR/cli.py" "\$@"
AGENTEOF
chmod +x "$TARGET/agent"
chmod +x "$TARGET/doctor.sh"
_ok "agent 실행 파일"

# ── 완료 ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  설치 완료!"
echo ""
echo "  다음 단계:"
echo "  1.  cd $TARGET"
echo "  2.  수정할 프로젝트 코드를 workspace/ 안에 둡니다"
echo "  3.  ./doctor.sh .          # 상태 확인"
echo "  4.  ollama serve           # Ollama 실행 (별도 터미널)"
echo "  5.  ./agent                # 에이전트 시작"
echo ""
echo "  * 에이전트는 workspace/ 안의 파일만 탐색/수정합니다."
echo "    (대상 폴더는 config.yaml 의 harness.work_dir 로 변경 가능)"
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
