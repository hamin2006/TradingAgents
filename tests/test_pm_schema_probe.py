"""pm_schema_probe tests (hermetic): prompt extraction from structured logs."""

import json

from pm_schema_probe import append_cards_block, extract_pm_prompt


def _llm_start(prompt_dump):
    return {"type": "llm_start", "agent": "Portfolio Manager",
            "prompt": prompt_dump}


def _write_logs(tmp_path, ticker, events):
    p = tmp_path / "structured" / "2026-09-04" / f"{ticker}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in events))
    return p


def test_extract_returns_last_pm_prompt_text(tmp_path):
    p = _write_logs(tmp_path, "HPE", [
        {"type": "llm_start", "agent": "Sentiment Analyst",
         "prompt": "[human] noise"},
        _llm_start("[system] system text\n[human] As the Portfolio Manager... plan"),
        _llm_start("[system] sys2\n[human] Second PM prompt body"),
    ])
    text = extract_pm_prompt(p)
    assert "Second PM prompt body" in text
    assert "[human]" not in text and "[system]" not in text


def test_extract_missing_file_none(tmp_path):
    from pathlib import Path
    assert extract_pm_prompt(Path(tmp_path) / "nope.jsonl") is None


def test_append_cards_block_joins_when_cards(tmp_path):
    import decision_cards
    root = tmp_path
    decision_cards.append_card(root, {
        "date": "2026-09-04", "ticker": "HPE", "rating": "Overweight",
        "executive_summary": "starter plan", "schema_version": 1})
    text = append_cards_block("prompt", "HPE", root, as_of="2026-09-05")
    assert text.startswith("prompt")
    assert "Prior PM decisions on HPE" in text


def test_append_cards_block_unchanged_when_none(tmp_path):
    assert append_cards_block("prompt", "HPE", tmp_path,
                              as_of="2026-09-05") == "prompt"
