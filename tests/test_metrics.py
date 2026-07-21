import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from harness import metrics


def test_redact_sensitive_masks_password_and_token():
    text = 'password: hunter2 and token="abc123"'
    result = metrics.redact_sensitive(text)
    assert "hunter2" not in result
    assert "abc123" not in result
    assert "[REDACTED]" in result


def test_redact_sensitive_leaves_normal_text_untouched():
    text = "add email validation to user.py"
    assert metrics.redact_sensitive(text) == text


def test_summarize_completion_rate_counts_completed_and_failed_only():
    records = [
        {"decision": "openrouter", "outcome": "completed"},
        {"decision": "openrouter", "outcome": "completed"},
        {"decision": "openrouter", "outcome": "failed"},
        {"decision": "local", "outcome": "explain"},
    ]
    s = metrics.summarize(records)
    assert s["completed"] == 2
    assert s["failed"] == 1
    assert s["attempted"] == 3
    assert round(s["completion_rate"], 2) == round(2 / 3, 2)


def test_summarize_completion_rate_none_when_no_attempts():
    records = [{"decision": "local", "outcome": "explain"}]
    s = metrics.summarize(records)
    assert s["attempted"] == 0
    assert s["completion_rate"] is None


def test_summarize_session_outcome_ignores_untagged_and_unknown():
    records = [
        {"decision": "external", "outcome": "interactive", "session_outcome": "success"},
        {"decision": "external", "outcome": "interactive", "session_outcome": "failure"},
        {"decision": "external", "outcome": "interactive", "session_outcome": "unknown"},
        {"decision": "external", "outcome": "interactive"},  # 옛 로그 — 필드 없음
    ]
    s = metrics.summarize(records)
    assert s["interactive"] == 4
    assert s["session_success"] == 1
    assert s["session_failure"] == 1
    assert s["session_tagged"] == 2
    assert s["session_success_rate"] == 0.5


def test_format_report_omits_completion_and_session_lines_when_absent():
    records = [{"decision": "local", "outcome": "chatter"}]
    s = metrics.summarize(records)
    report = metrics.format_report(s)
    assert "완료율" not in report
    assert "세션 성공률" not in report


def test_format_report_shows_completion_rate_when_present():
    records = [
        {"decision": "openrouter", "outcome": "completed"},
        {"decision": "openrouter", "outcome": "failed"},
    ]
    s = metrics.summarize(records)
    report = metrics.format_report(s)
    assert "코드 변경 완료율" in report
    assert "완료 1 / 실패 1" in report
