import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from harness import context


def test_format_conversation_history_empty_returns_empty_string():
    assert context.format_conversation_history(None) == ""
    assert context.format_conversation_history([]) == ""


def test_format_conversation_history_includes_recent_turns():
    hist = ["tools 폴더에 autobuild.exe 실행해보고 결과 알려줘"]
    result = context.format_conversation_history(hist)
    assert "autobuild.exe" in result
    assert "직전 대화" in result


def test_format_conversation_history_limits_to_recent_n():
    hist = [f"작업 {i}" for i in range(10)]
    result = context.format_conversation_history(hist, limit=5)
    assert "작업 9" in result
    assert "작업 0" not in result


def test_select_relevant_files_prompt_includes_conversation(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")
    captured = {}

    class DummyLLM:
        def generate(self, prompt, json_mode=False, system=None, num_predict=512):
            captured["prompt"] = prompt
            return []

    context.select_relevant_files(
        DummyLLM(), "직접 실행을 왜 못해?", str(tmp_path),
        conversation_history=["autobuild.exe 실행해줘"],
    )
    assert "autobuild.exe" in captured["prompt"]
