#!/usr/bin/env bash
# AI Agent 진단 스크립트
# Usage: ./doctor.sh [프로젝트 디렉토리]
set -uo pipefail

TARGET="${1:-.}"
TARGET="$(cd "$TARGET" 2>/dev/null && pwd)" || {
    echo "오류: 디렉토리를 찾을 수 없습니다: $1"
    exit 1
}

PASS=0; FAIL=0; WARN=0

_ok()   { printf "  \033[32m✓\033[0m  %-28s %s\n" "$1" "${2:-}"; PASS=$((PASS+1)); }
_fail() { printf "  \033[31m✗\033[0m  %-28s %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
_warn() { printf "  \033[33m⚠\033[0m  %-28s %s\n" "$1" "${2:-}"; WARN=$((WARN+1)); }
_sect() { printf "\n  \033[1m%s\033[0m\n" "$1"; }

echo ""
echo "  ╔════════════════════════════════════════╗"
echo "  ║  AI Agent 진단                         ║"
printf "  ║  경로: %-33s║\n" "$TARGET"
echo "  ╚════════════════════════════════════════╝"

# ── 환경 ──────────────────────────────────────────────────────────────────────
_sect "환경"

if command -v python3 &>/dev/null; then
    PV=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PM=$(echo "$PV" | cut -d. -f1)
    Pm=$(echo "$PV" | cut -d. -f2)
    if [[ $PM -ge 3 && $Pm -ge 10 ]]; then
        _ok "Python" "$PV"
    else
        _fail "Python" "$PV (3.10+ 필요)"
    fi
else
    _fail "Python 3" "없음"
fi

if [[ -d "$TARGET/.venv" ]]; then
    _ok "가상환경" ".venv"
else
    _fail "가상환경" "없음 — install.sh 재실행 필요"
fi

# ── 파일 구조 ─────────────────────────────────────────────────────────────────
_sect "파일 구조"

for f in agent.py cli.py router.py config.yaml requirements.txt; do
    if [[ -f "$TARGET/$f" ]]; then
        _ok "$f"
    else
        _fail "$f" "없음"
    fi
done

for d in backends harness src; do
    if [[ -d "$TARGET/$d" ]]; then
        _ok "$d/"
    else
        _fail "$d/" "없음"
    fi
done

if [[ -f "$TARGET/agent" && -x "$TARGET/agent" ]]; then
    _ok "agent 실행 파일"
else
    _fail "agent 실행 파일" "없거나 실행 권한 없음"
fi

# ── Python 패키지 ─────────────────────────────────────────────────────────────
_sect "패키지"

if [[ -d "$TARGET/.venv" ]]; then
    for pkg in rich requests yaml; do
        import_name="$pkg"
        [[ "$pkg" == "yaml" ]] && import_name="yaml"  # pyyaml → yaml
        if "$TARGET/.venv/bin/python" -c "import $import_name" 2>/dev/null; then
            _ok "$pkg"
        else
            _fail "$pkg" "pip install -r requirements.txt 실행"
        fi
    done
else
    _warn "패키지 확인 불가" "가상환경 없음"
fi

# ── Ollama ────────────────────────────────────────────────────────────────────
_sect "Ollama"

# config 파싱은 venv python(yaml 있음)으로, 없으면 기본값 사용
_PYTHON="${TARGET}/.venv/bin/python"
[[ -x "$_PYTHON" ]] || _PYTHON="python3"

OLLAMA_URL="http://192.168.0.229:11345"
if [[ -f "$TARGET/config.yaml" ]]; then
    OLLAMA_URL=$("$_PYTHON" -c "
import yaml
with open('$TARGET/config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('local_llm', {}).get('base_url', 'http://192.168.0.229:11345'))
" 2>/dev/null || echo "http://192.168.0.229:11345")
fi

TAGS_FILE="/tmp/._agent_doctor_tags.json"
if curl -sf "$OLLAMA_URL/api/tags" -o "$TAGS_FILE" 2>/dev/null; then
    _ok "Ollama 서버" "$OLLAMA_URL"

    if [[ -f "$TARGET/config.yaml" ]]; then
        MODELS=$("$_PYTHON" -c "
import yaml
with open('$TARGET/config.yaml') as f:
    c = yaml.safe_load(f)
llm = c.get('local_llm', {})
print(llm.get('model', ''))
print(llm.get('router_model', ''))
" 2>/dev/null || echo "")

        MAIN_MODEL=$(echo "$MODELS" | sed -n '1p')
        ROUTER_MODEL=$(echo "$MODELS" | sed -n '2p')

        AVAILABLE=$("$_PYTHON" -c "
import json
with open('$TAGS_FILE') as f:
    d = json.load(f)
print(' '.join(m['name'] for m in d.get('models', [])))
" 2>/dev/null || echo "")

        _check_model() {
            local model="$1" label="$2"
            local prefix="${model%%:*}"
            if echo "$AVAILABLE" | grep -qw "$model" 2>/dev/null || \
               echo "$AVAILABLE" | grep -q "$prefix" 2>/dev/null; then
                _ok "모델: $model" "$label"
            else
                _warn "모델: $model" "$label — ollama pull $model"
            fi
        }

        [[ -n "$MAIN_MODEL" ]]   && _check_model "$MAIN_MODEL"   "메인"
        [[ -n "$ROUTER_MODEL" && "$ROUTER_MODEL" != "$MAIN_MODEL" ]] \
            && _check_model "$ROUTER_MODEL" "라우터"
    fi
    rm -f "$TAGS_FILE"
else
    _warn "Ollama 서버" "연결 안 됨 — ollama serve 실행 필요"
fi

# ── 기타 ──────────────────────────────────────────────────────────────────────
_sect "기타"

[[ -f "$TARGET/.env" ]] && _ok ".env" || _warn ".env" "없음 (필요시 생성)"
[[ -d "$TARGET/memory" ]] && _ok "memory/" || _warn "memory/" "없음"

# ── 요약 ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "  결과:  \033[32m✓ %d 통과\033[0m  \033[31m✗ %d 실패\033[0m  \033[33m⚠ %d 경고\033[0m\n" \
    "$PASS" "$FAIL" "$WARN"
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

[[ $FAIL -gt 0 ]] && exit 1 || exit 0
