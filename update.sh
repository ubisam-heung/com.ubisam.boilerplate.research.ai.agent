#!/usr/bin/env bash
# AI Agent 업데이트 스크립트 — 이미 설치된 프로젝트에 프레임워크 코드만 다시 덮어쓴다.
# config.yaml, AGENTS.md, memory/, sessions/, workspace/, .venv, .env 등 프로젝트 고유 상태는 건드리지 않는다.
# Usage: ./update.sh --target <디렉토리>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INVOKE_DIR="$PWD"   # 명령어를 입력한 터미널의 현재 위치 (상대경로 --target의 기준점)
TARGET=""

# ── 인수 파싱 ─────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target|-t) TARGET="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 --target <dir>"
            exit 0
            ;;
        *)
            echo "알 수 없는 옵션: $1"
            echo "Usage: $0 --target <dir>"
            exit 1
            ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    echo "오류: --target 옵션이 필요합니다."
    echo "Usage: $0 --target <dir>"
    exit 1
fi

# ~ 확장
TARGET="${TARGET/#\~/$HOME}"
# 절대경로가 아니면 명령어를 입력한 터미널 위치의 한 단계 위(부모) 기준으로 해석한다.
if [[ "$TARGET" != /* ]]; then
    TARGET="$(dirname "$INVOKE_DIR")/$TARGET"
fi

if [[ ! -d "$TARGET" ]]; then
    echo "오류: 대상 디렉토리가 없습니다: $TARGET"
    echo "      (처음 설치하는 거라면 install.sh를 사용하세요)"
    exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

# ── 유틸 ──────────────────────────────────────────────────────────────────────
_ok()   { printf "  \033[32m✓\033[0m  %s\n" "$1"; }
_fail() { printf "  \033[31m✗\033[0m  %s\n" "$1" >&2; }
_step() { printf "  \033[34m▸\033[0m  %s\n" "$1"; }

echo ""
echo "  ╔════════════════════════════════════════╗"
echo "  ║  AI Agent 업데이트                     ║"
printf "  ║  대상: %-33s║\n" "$TARGET"
echo "  ╚════════════════════════════════════════╝"
echo ""

if [[ "$TARGET" == "$SCRIPT_DIR" ]]; then
    _fail "대상이 프레임워크 원본 폴더와 동일합니다."
    exit 1
fi

# ── 프레임워크 파일 덮어쓰기 ───────────────────────────────────────────────────
# config.yaml / AGENTS.md / memory / sessions / workspace 등 프로젝트별 상태는 제외.
_step "프레임워크 파일 갱신 중..."

FILES=(agent.py cli.py router.py metrics.py conftest.py doctor.sh requirements.txt)
DIRS=(backends harness src)

for f in "${FILES[@]}"; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$TARGET/$f"
        _ok "$f"
    fi
done

for d in "${DIRS[@]}"; do
    if [[ -d "$SCRIPT_DIR/$d" ]]; then
        rm -rf "$TARGET/$d"
        cp -r "$SCRIPT_DIR/$d" "$TARGET/$d"
        _ok "$d/"
    fi
done

# __pycache__ 정리
find "$TARGET" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

chmod +x "$TARGET/doctor.sh" 2>/dev/null || true

# ── 의존성 재설치 (requirements.txt가 갱신됐으므로) ─────────────────────────────
if [[ -x "$TARGET/.venv/bin/pip" ]]; then
    _step "의존성 동기화 중..."
    "$TARGET/.venv/bin/pip" install --quiet -r "$TARGET/requirements.txt"
    _ok "패키지 동기화 완료"
else
    echo "  [dim](.venv 없음 — 의존성 동기화는 건너뜀)[/dim]"
fi

# ── 완료 ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  업데이트 완료!"
echo ""
echo "  변경하지 않은 항목: config.yaml, AGENTS.md, memory/, sessions/, workspace/, .env"
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
