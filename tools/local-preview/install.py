#!/usr/bin/env python3
"""Upgrade an existing macOS local preview installation, preserving its data."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        action="store_true",
        help="Start the installed watcher after installation",
    )
    args = parser.parse_args()
    if not os.environ.get("DLR_PREVIEW_HOME"):
        parser.error("Set DLR_PREVIEW_HOME to your private installation directory")
    root = preview.ROOT
    source = Path(__file__).resolve().parent
    config = preview.read("config.json")
    state = preview.read("state.json")
    if not config or not state or not state.get("sha"):
        parser.error(
            "This installer upgrades an existing local preview installation; see docs for prerequisites"
        )
    preview.settings()
    if preview.read("attention.json"):
        parser.error(
            "Resolve the unfinished deployment before replacing the controller"
        )
    backup = (
        root
        / "controller-backups"
        / datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%f")
    )
    backup.mkdir(parents=True, mode=0o700)
    for file in root.iterdir():
        if file.is_file() and file.suffix in {".py", ".sh", ".json", ".env", ".md"}:
            shutil.copy2(file, backup / file.name)
    plist = (
        Path.home() / "Library/LaunchAgents" / (config["launchagent_label"] + ".plist")
    )
    shutil.copy2(plist, backup / plist.name)
    # Pause before unloading, then acquire the controller lock: never replace in-flight code.
    with preview.config_lock():
        paused = dict(config, enabled=False)
        preview.write("config.json", paused)
    message = (preview.read("status.json", {}) or {}).get("message", "").lower()
    if any(
        word in message for word in ("deploying", "recovering", "building", "switching")
    ):
        parser.error(
            "Controller is active; it is now paused. Wait for the current operation to finish and rerun install."
        )
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
        capture_output=True,
        check=False,
    )
    with (root / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in (
            "preview.py",
            "migrations.py",
            "deploy.sh",
            "verify.py",
            "assets.py",
        ):
            shutil.copy2(source / name, root / name)
        shutil.copy2(
            source.parents[1] / "docs/zh-CN/local-preview.md", root / "README.md"
        )
        # Use stdin to transfer trusted code; never expose the environment file.
        for name in ("deploy.sh", "verify.py", "assets.py"):
            subprocess.run(
                [
                    preview.COLIMA,
                    "ssh",
                    "-p",
                    config["profile"],
                    "--",
                    "sudo",
                    "tee",
                    preview.vm_path(name),
                ],
                input=(source / name).read_bytes(),
                stdout=subprocess.DEVNULL,
                env=preview.ENV,
                check=True,
            )
        deployment = {
            key: config[key]
            for key in (
                "project",
                "web_port",
                "sandbox_unit",
                "sandbox_cpu_quota",
                "sandbox_memory_max",
            )
        }
        preview.vm_command(
            "install", "-m", "600", "/dev/null", preview.vm_path("deployment.json")
        )
        subprocess.run(
            [
                preview.COLIMA,
                "ssh",
                "-p",
                config["profile"],
                "--",
                "sudo",
                "tee",
                preview.vm_path("deployment.json"),
            ],
            input=json.dumps(deployment),
            text=True,
            stdout=subprocess.DEVNULL,
            env=preview.ENV,
            check=True,
        )
        with plist.open("rb") as source_plist:
            launch = plistlib.load(source_plist)
        launch.setdefault("EnvironmentVariables", {})["DLR_PREVIEW_HOME"] = str(root)
        with plist.open("wb") as output_plist:
            plistlib.dump(launch, output_plist)
        if not (root / "installation.json").exists():
            preview.phase(state, "adopt")
        installation = {
            "installed_at": datetime.datetime.now().astimezone().isoformat(),
            "backup": str(backup),
            "source": str(source),
            "files": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    "preview.py",
                    "migrations.py",
                    "deploy.sh",
                    "verify.py",
                    "assets.py",
                    "README.md",
                )
            },
        }
        preview.write("installation.json", installation)
        with preview.config_lock():
            preview.write("config.json", config)
    if args.start:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True
        )
    print(json.dumps(installation, indent=2))


if __name__ == "__main__":
    main()
