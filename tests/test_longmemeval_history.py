import json

import pytest

from benchmarks import longmemeval_history as history


def test_chunk_boundaries_preserve_conversations(tmp_path):
    original = [{"content": "a } ] , \\"}, {"content": "日本語\n"}]
    path = tmp_path / "array.json"
    path.write_text(json.dumps(original, ensure_ascii=False))
    assert list(history.iter_json_array(path, chunk_size=2)) == original


@pytest.mark.parametrize("text", ['[{"a":', '[{} {}]', '[{}] trailing'])
def test_partial_or_invalid_source_is_rejected(tmp_path, text):
    path = tmp_path / "array.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        list(history.iter_json_array(path, chunk_size=3))


def example():
    return {"haystack_sessions": [
        [{"role": "user", "content": "question", "has_answer": False},
         {"role": "assistant", "content": "complete answer", "has_answer": True}],
        [{"role": "user", "content": "different", "has_answer": False}]],
        "haystack_dates": ["2023/04/10 (Mon) 04:32", "2023/04/11 (Tue) 05:00"],
        "answer": "complete answer", "answer_session_ids": ["gold"]}


def test_restore_preserves_assistant_and_date_without_oracle_labels():
    result = history.restore_session("scene_0_session_1", "question", example())
    assert result == {"session_date": "2023/04/10 (Mon) 04:32", "turns": [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "complete answer"}]}
    assert "has_answer" not in json.dumps(result)


def test_wrong_session_or_modified_body_fails_alignment():
    with pytest.raises(ValueError, match="Source mismatch"):
        history.restore_session("scene_0_session_2", "question", example())
    with pytest.raises(ValueError, match="Source mismatch"):
        history.restore_session("scene_0_session_1", "wrong text", example())
