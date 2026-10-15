"""
The README's quick example must run exactly as written and print what it says.
"""

import contextlib
import io
import pathlib
import re

README = pathlib.Path(__file__).resolve().parent.parent / "README.md"


def _quick_example() -> str:
    text = README.read_text(encoding="utf-8")
    section = text.split("## Quick example", 1)[1]
    match = re.search(r"```python\n(.*?)```", section, re.S)
    assert match, "README quick example not found"
    return match.group(1)


def test_readme_quick_example_runs_and_commits():
    code = _quick_example()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(code, "README.md", "exec"), {"__name__": "readme_example"})
    lines = output.getvalue().strip().splitlines()
    assert lines == ["COMMITTED - Transaction committed successfully", "(True, None)"]


def test_readme_links_point_at_existing_tests():
    text = README.read_text(encoding="utf-8")
    root = README.parent
    for path in set(re.findall(r"\]\(((?:tests|examples|docs)/[^)#]+)\)", text)):
        assert (root / path).exists(), path
    # Every test named in the guarantees table exists in the file it links to
    # (a `::name` entry continues the file named before it on the same row)
    checked = 0
    for row in (line for line in text.splitlines() if line.startswith("| ")):
        current = None
        for item in re.findall(r"`([^`]+)`", row):
            match = re.fullmatch(r"(test_\w+\.py)?((?:::\w+)*)", item)
            if not match or not (match.group(1) or match.group(2)):
                continue
            current = match.group(1) or current
            names = [n for n in match.group(2).split("::") if n]
            if current and names:
                source = (root / "tests" / current).read_text(encoding="utf-8")
                for name in names:
                    keyword = "class" if name.startswith("Test") else "def"
                    assert re.search(rf"^\s*{keyword} {name}\b", source, re.M), f"{current}::{name}"
                    checked += 1
    assert checked >= 25


def test_readme_state_counts_are_true():
    from cfa_shoc import TransactionState, TransactionStateMachine

    text = README.read_text(encoding="utf-8")
    assert f"{len(TransactionState)} states and {len(TransactionStateMachine.LEGAL_TRANSITIONS)} legal transitions" in text
