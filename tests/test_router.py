import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from router import (
    pick_external_tool,
    pick_openrouter_model,
    score_task_complexity,
    tool_enabled,
)


def test_score_task_complexity_orders_easy_below_hard():
    easy = score_task_complexity("오타 수정해줘")
    hard = score_task_complexity(
        "여러 파일에 걸친 대규모 아키텍처 리팩토링과 동시성 버그 원인을 분석해서 고쳐줘"
    )
    assert easy < hard


def test_score_task_complexity_empty_task_is_zero():
    assert score_task_complexity("") == 0
    assert score_task_complexity(None) == 0


def test_pick_openrouter_model_respects_max_score_order():
    cfg = {
        "openrouter": {
            "models": [
                {"name": "cheap", "max_score": 3},
                {"name": "mid", "max_score": 8},
                {"name": "strong", "max_score": 999},
            ]
        }
    }
    model, score = pick_openrouter_model(cfg, "오타 수정")
    assert model == "cheap"

    model, score = pick_openrouter_model(
        cfg,
        "여러 파일(a.py, b.py, c.py)에 걸친 대규모 아키텍처 리팩토링과 마이그레이션이 필요하고, "
        "동시성 race condition 버그의 원인을 디버그해서 성능 최적화와 보안 취약점 점검까지 "
        "함께 진행해야 하는 복잡한 작업이다. 통합 테스트도 새로 작성해야 한다.",
    )
    assert model == "strong"


def test_pick_openrouter_model_falls_back_to_fixed_model_when_no_list():
    cfg = {"openrouter": {"model": "fixed-model", "models": []}}
    model, _ = pick_openrouter_model(cfg, "아무 작업")
    assert model == "fixed-model"


def test_tool_enabled_defaults_true_when_unspecified():
    assert tool_enabled({}, "claude_code") is True


def test_tool_enabled_respects_explicit_false():
    cfg = {"external_tools": {"codex": {"enabled": False}}}
    assert tool_enabled(cfg, "codex") is False


def test_pick_external_tool_prefers_enabled_default():
    cfg = {
        "external_tools": {
            "default": "codex",
            "claude_code": {"enabled": True},
            "codex": {"enabled": True},
        }
    }
    assert pick_external_tool(cfg) == "codex"


def test_pick_external_tool_skips_disabled_default():
    cfg = {
        "external_tools": {
            "default": "codex",
            "claude_code": {"enabled": True},
            "codex": {"enabled": False},
        }
    }
    assert pick_external_tool(cfg) == "claude_code"


def test_pick_external_tool_returns_none_when_all_disabled():
    cfg = {
        "external_tools": {
            "claude_code": {"enabled": False},
            "codex": {"enabled": False},
        }
    }
    assert pick_external_tool(cfg) is None
