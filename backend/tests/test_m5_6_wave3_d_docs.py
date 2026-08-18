"""M5.6 Wave 3 D contracts: bilingual README/docs pairing and i18n resource parity.

These tests only guard the current bilingual documentation set; historical specs,
migrations and UI acceptance evidence are deliberately outside the pairing contract.
"""

import re
from pathlib import Path
from string import Formatter

from dlr.worker.i18n import _MESSAGES

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DOC_PAIRS = (
    ("README.md", "README.zh-CN.md"),
    ("docs/en/product.md", "docs/zh-CN/product.md"),
    ("docs/en/architecture.md", "docs/zh-CN/architecture.md"),
)

CHECKED_FILES = tuple(dict.fromkeys(path for pair in REQUIRED_DOC_PAIRS for path in pair))

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6}) ")


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _header(text: str) -> str:
    """Everything before the first H2 heading (title + language links)."""
    return text.split("\n## ", 1)[0]


def _heading_counts(text: str) -> list[int]:
    counts = [0] * 6
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match is not None:
            counts[len(match.group(1)) - 1] += 1
    return counts


def test_required_bilingual_docs_exist_in_pairs() -> None:
    for en_path, zh_path in REQUIRED_DOC_PAIRS:
        en = REPO_ROOT / en_path
        zh = REPO_ROOT / zh_path
        assert en.is_file() and en.read_text(encoding="utf-8").strip(), f"{en_path} missing/empty"
        assert zh.is_file() and zh.read_text(encoding="utf-8").strip(), f"{zh_path} missing/empty"


def test_readme_mutual_language_links_are_prominent() -> None:
    readme_header = _header(_read("README.md"))
    readme_zh_header = _header(_read("README.zh-CN.md"))
    assert "[简体中文](README.zh-CN.md)" in readme_header
    assert "[English](README.md)" in readme_zh_header


def test_doc_pair_heading_structure_is_aligned() -> None:
    for en_path, zh_path in REQUIRED_DOC_PAIRS:
        assert _heading_counts(_read(en_path)) == _heading_counts(_read(zh_path)), (
            f"heading structure mismatch between {en_path} and {zh_path}"
        )


def test_relative_markdown_links_resolve() -> None:
    for relative in CHECKED_FILES:
        base = REPO_ROOT / relative
        for raw_target in MARKDOWN_LINK_RE.findall(_read(relative)):
            target = raw_target.strip().split()[0]
            if target.startswith(("http://", "https://", "mailto:")) or target.startswith("#"):
                continue
            target_path = (base.parent / target.split("#", 1)[0]).resolve()
            assert target_path.exists(), f"{relative}: broken relative link -> {raw_target.strip()}"


def test_worker_i18n_message_tables_have_identical_keys_and_placeholders() -> None:
    zh = _MESSAGES["zh-CN"]
    en = _MESSAGES["en"]
    assert set(zh) == set(en)
    for key in zh:
        zh_fields = {field for _, field, _, _ in Formatter().parse(zh[key]) if field}
        en_fields = {field for _, field, _, _ in Formatter().parse(en[key]) if field}
        assert zh_fields == en_fields, f"placeholder mismatch for worker message {key}"
