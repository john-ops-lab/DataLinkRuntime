"""Generate public B0 metadata, without reading credentials or local deployment state."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import platform
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from probe_support import FINAL_TEXT, PNG, pdf

BASE = "5fc95945b894fbc07aae7fc873aac91a2e26fabc"
ORIGINAL = "7fb4c8fc00a0a167a65a95f18e310b9208f1e3ed"
ROOT = Path(__file__).resolve().parents[2]
FROZEN_FILES = (
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/src/dlr/control/api/ai.py",
    "backend/src/dlr/control/schemas/ai.py",
    "backend/src/dlr/control/services/ai.py",
    "backend/src/dlr/control/ai/providers.py",
    "backend/src/dlr/control/ai/tools.py",
    "backend/src/dlr/control/ai/context_budget.py",
    "backend/src/dlr/control/ai/prompt_builder.py",
    "backend/src/dlr/control/ai/prompts/system.md",
    "backend/src/dlr/control/ai/attachment_reading.py",
    "web/src/components/AiAssistantPanel.tsx",
    "docs/testing/issue215-ai-assistant.md",
    "openspec/changes/issue215-ai-assistant-ux/specs/ai-code-comment-language/spec.md",
)


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def license_summary(dist: metadata.Distribution) -> str:
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        value.removeprefix("License :: ")
        for value in dist.metadata.get_all("Classifier", [])
        if value.startswith("License :: ")
    ]
    if classifiers:
        return "; ".join(classifiers)
    license_text = dist.metadata.get("License", "")
    if license_text.startswith("MIT License"):
        return "MIT"
    if license_text and len(license_text) < 160:
        return license_text
    return "See installed distribution license files; not normalized automatically"


def snapshot() -> dict[str, Any]:
    baseline = tomllib.loads(git("show", BASE + ":backend/uv.lock").decode())
    baseline_versions = {
        package["name"]: package["version"]
        for package in baseline["package"]
        if "version" in package
    }
    probe_lock = tomllib.loads((ROOT / "tools/ai-migration/uv.lock").read_text())
    packages: list[dict[str, Any]] = []
    for package in probe_lock["package"]:
        name = package["name"]
        if name in {"dlr", "dlr-ai-migration-probe"}:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            packages.append(
                {
                    "name": name,
                    "version": package["version"],
                    "installed": False,
                    "reason": "conditional dependency not installed on this platform",
                }
            )
            continue
        # metadata.files may contain relative paths; no absolute host path is emitted.
        installed_bytes = sum(
            path.stat().st_size
            for entry in dist.files or []
            if (path := Path(str(dist.locate_file(entry)))).is_file()
        )
        old = baseline_versions.get(name)
        packages.append(
            {
                "name": name,
                "version": package["version"],
                "installed": True,
                "baseline_version": old,
                "change": "new"
                if old is None
                else ("same" if old == package["version"] else "changed"),
                "license": license_summary(dist),
                "installed_file_bytes": installed_bytes,
            }
        )
    changed = git("diff", "--name-only", ORIGINAL, BASE).decode().splitlines()
    return {
        "baseline_sha": BASE,
        "original_design_sha": ORIGINAL,
        "python": platform.python_version(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "frozen_files": {path: sha256(git("show", BASE + ":" + path)) for path in FROZEN_FILES},
        "changed_files_since_original": changed,
        "probe_lock_sha256": sha256((ROOT / "tools/ai-migration/uv.lock").read_bytes()),
        "synthetic_material_sha256": {
            "one-pixel.png": sha256(PNG),
            "one-page.pdf": sha256(pdf()),
            "final-json": sha256(FINAL_TEXT.encode()),
        },
        "unchanged_installed_baseline_packages": sum(
            package.get("change") == "same" for package in packages
        ),
        "packages": sorted(
            [package for package in packages if package.get("change") != "same"],
            key=lambda package: package["name"],
        ),
        "historical_evidence": {
            "issue215": {
                "record": "docs/testing/issue215-ai-assistant.md",
                "preserved_failures": [
                    "Go opposite-language comments",
                    "Python explanation and implementation disagree",
                    "TypeScript blank amount treated as zero",
                    "Chinese server / English page attachment Regenerate: ai_tool_duplicate",
                ],
                "latest_help_entry_tests": "not rerun in the recorded final UI adjustment",
            },
            "migration_baseline_acceptance": "not rerun; B0 is isolated synthetic evidence",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write metadata JSON; omit to print")
    options = parser.parse_args()
    value = snapshot()
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if options.output is not None:
        options.output.write_text(rendered)
        print("B0 baseline metadata written")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
