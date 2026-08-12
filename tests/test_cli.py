import argparse

from agent_deck.cli import _read_text


def test_read_text_requires_one_source(tmp_path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("from file", encoding="utf-8")
    assert _read_text(None, str(prompt), "prompt") == "from file"
    assert _read_text("inline", None, "prompt") == "inline"


def test_namespace_import_is_available() -> None:
    assert argparse.Namespace is not None
