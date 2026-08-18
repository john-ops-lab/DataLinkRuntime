"""M5.6 Wave 4 E contracts: repository-wide scan for DLR user-visible
hardcoded natural language.

The scan is a regression guard for the i18n contract:

- ``web/src`` must not contain user-visible Chinese or English sentence
  literals outside the bundled locale resources (comments, docstrings,
  technical identifiers and code samples are allowed);
- ``backend/src`` must not contain DLR-generated user-visible Chinese outside
  the canonical message tables, preset display names and compatibility
  messages that the frontend localizes by stable code / preset id.

Historical docs, test data, user stdout/stderr/Traceback handling and
third-party tool output are deliberately outside this scan. The backend
English compatibility ``message`` fields of ``domain_error`` are a stable
machine contract and remain allowed as well.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "web" / "src"
BACKEND_ROOT = REPO_ROOT / "backend" / "src"

CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")

# --- web -------------------------------------------------------------------

WEB_EXCLUDED_SUFFIXES = (".test.ts", ".test.tsx")
WEB_EXCLUDED_NAMES = ("test-setup.ts",)
WEB_EXCLUDED_RELATIVE_DIRS = ("i18n/locales",)

# SystemSettingsDrawer reachability classification regexes match
# server/provider error text; they classify, they are never displayed.
WEB_LINE_ALLOWLIST_CHINESE = (".test(errorDetail",)

# JSX text-node scan: a real translation call on the same line covers the
# expression-tail false positive (`=> t(key, options)`), and the credential
# binding editor renders the `context.secrets.get("PASSWORD")` code sample.
WEB_LINE_ALLOWLIST_JSX_TEXT = ("context.secrets.get",)

JSX_TEXT_NODE_RE = re.compile(r">([^<{]*[A-Za-z][^<{]*)<")
JSX_PROPS_RE = re.compile(
    r'\b(?:title|placeholder|label|description|aria-label|tooltip|okText|cancelText)="([^"]*)"'
)

STRING_LITERAL_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`', re.S)
BLOCK_COMMENT_RE = re.compile(r"/\*(?:(?!\*/)[^\\]|\\.)*?\*/", re.S)


def _web_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(WEB_ROOT.rglob("*")):
        if path.suffix not in (".ts", ".tsx"):
            continue
        if path.name.endswith(WEB_EXCLUDED_SUFFIXES) or path.name in WEB_EXCLUDED_NAMES:
            continue
        if any(part in WEB_EXCLUDED_RELATIVE_DIRS for part in path.relative_to(WEB_ROOT).parts):
            continue
        files.append(path)
    return files


def _strip_ts_comments_and_strings(code: str) -> str:
    """Naive but adequate stripping: strings first, then block/line comments."""
    code = STRING_LITERAL_RE.sub("", code)
    code = BLOCK_COMMENT_RE.sub("", code)
    lines = []
    for line in code.splitlines():
        lines.append(re.sub(r"//.*$", "", line))
    return "\n".join(lines)


def test_web_has_no_user_visible_hardcoded_chinese() -> None:
    violations: list[str] = []
    for path in _web_files():
        code = _strip_ts_comments_and_strings(path.read_text(encoding="utf-8"))
        for index, line in enumerate(code.splitlines(), 1):
            if not CHINESE_RE.search(line):
                continue
            if any(marker in line for marker in WEB_LINE_ALLOWLIST_CHINESE):
                continue
            violations.append(f"{path.relative_to(REPO_ROOT)}:{index}: {line.strip()[:120]}")
    assert not violations, (
        "user-visible hardcoded Chinese found outside the locale resources:\n"
        + "\n".join(violations)
    )


def test_web_has_no_user_visible_hardcoded_english_jsx_text() -> None:
    violations: list[str] = []
    for path in _web_files():
        if path.suffix != ".tsx":
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "t(" in line or any(marker in line for marker in WEB_LINE_ALLOWLIST_JSX_TEXT):
                continue
            for match in JSX_TEXT_NODE_RE.finditer(line):
                text = match.group(1).strip()
                words = re.findall(r"[A-Za-z]{2,}", text)
                if len(words) >= 2:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{index}: {text[:120]}")
    assert not violations, (
        "user-visible hardcoded English JSX text found; translate it via t():\n"
        + "\n".join(violations)
    )


def test_web_has_no_user_visible_hardcoded_english_props() -> None:
    violations: list[str] = []
    for path in _web_files():
        if path.suffix != ".tsx":
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in JSX_PROPS_RE.finditer(line):
                value = match.group(1)
                # UI copy is sentence-like (contains spaces); URLs, tokens and
                # single technical words are identifiers, not translated copy.
                if " " in value and re.search(r"[a-z]{3,}", value):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{index}: {value[:120]}")
    assert not violations, (
        "user-visible hardcoded English JSX props found; translate them via t():\n"
        + "\n".join(violations)
    )


# --- backend ---------------------------------------------------------------

# Canonical zh message tables; zh/en parity is asserted by the Wave 3 D test.
# Preset display names: the frontend localizes them by stable preset_id.
# ai.py compatibility messages: the frontend localizes by stable error code;
# the message field stays a zh-CN compatibility fallback by design.
# tools.py: the deterministic truncation marker is data inside sanitized tool
# results (the localized "truncated" notice is the UI chrome), exactly like
# the ai.py compatibility-message contract.
BACKEND_ALLOWLISTED_FILES = (
    "dlr/worker/i18n.py",
    "dlr/control/package_source_defaults.py",
    "dlr/control/services/ai.py",
    "dlr/control/ai/tools.py",
)

# Canonical zh dependency events emitted by the env managers; the executor
# localizes them to the Execution locale before they reach the unified log.
# The "平台默认" suffix only names a stored row on name collision and is never
# displayed while the row still carries its preset_id.
BACKEND_LINE_ALLOWLIST = ("dependency_log(", "平台默认")

PY_DOCSTRING_RE = re.compile(
    r'"""(?:(?!""")[^\\]|\\.)*?"""|\'\'\'(?:(?!\'\'\')[^\\]|\\.)*?\'\'\'', re.S
)


def _backend_files() -> list[Path]:
    return sorted(BACKEND_ROOT.rglob("*.py"))


def _strip_py_comments(code: str) -> str:
    code = PY_DOCSTRING_RE.sub("", code)
    lines = []
    for line in code.splitlines():
        lines.append(re.sub(r"#.*$", "", line))
    return "\n".join(lines)


def test_backend_has_no_user_visible_hardcoded_chinese() -> None:
    violations: list[str] = []
    for path in _backend_files():
        relative = path.relative_to(BACKEND_ROOT).as_posix()
        if relative in BACKEND_ALLOWLISTED_FILES:
            continue
        code = _strip_py_comments(path.read_text(encoding="utf-8"))
        for index, line in enumerate(code.splitlines(), 1):
            if not CHINESE_RE.search(line):
                continue
            if any(marker in line for marker in BACKEND_LINE_ALLOWLIST):
                continue
            violations.append(f"{path.relative_to(REPO_ROOT)}:{index}: {line.strip()[:120]}")
    assert not violations, (
        "DLR-generated user-visible Chinese found in backend code outside the "
        "canonical tables / compatibility messages:\n" + "\n".join(violations)
    )
