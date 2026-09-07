#!/usr/bin/env python3
"""CI-gated local preview controller. PR code executes only in an unshared VM."""

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from migrations import compatible

ROOT = Path(os.environ.get("DLR_PREVIEW_HOME", "."))
ENV = {
    k: v
    for k, v in os.environ.items()
    if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
}
ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
GH = "/opt/homebrew/bin/gh"
COLIMA = "/opt/homebrew/bin/colima"


def read(name, default=None):
    path = ROOT / name
    return json.loads(path.read_text()) if path.exists() else default


def settings():
    """Deployment identity belongs to private config, never repository defaults."""
    config = read("config.json", {})
    patterns = {
        "profile": r"[a-zA-Z0-9][a-zA-Z0-9_.-]*",
        "project": r"[a-z0-9][a-z0-9_-]*",
        "vm_root": r"/[a-zA-Z0-9_./-]+",
        "launchagent_label": r"[a-zA-Z0-9][a-zA-Z0-9_.-]*",
        "sandbox_unit": r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.service",
        "sandbox_cpu_quota": r"[1-9][0-9]*%",
        "sandbox_memory_max": r"[1-9][0-9]*[KMG]",
    }
    for key, pattern in patterns.items():
        if not re.fullmatch(pattern, str(config.get(key, ""))):
            raise ValueError(f"Missing or invalid private configuration: {key}")
    if ".." in Path(config["vm_root"]).parts or config["vm_root"].endswith("/"):
        raise ValueError("vm_root must be a normalized installation directory")
    port = config.get("web_port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Private configuration must supply web_port")
    return config


def vm_path(suffix):
    return settings()["vm_root"] + "/" + suffix


def container(service):
    return settings()["project"] + "-" + service + "-1"


def write(name, data):
    path = ROOT / name
    with tempfile.NamedTemporaryFile(mode="w", dir=ROOT, delete=False) as output:
        os.chmod(output.name, 0o600)
        json.dump(data, output, indent=2, ensure_ascii=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(output.name, path)


@contextlib.contextmanager
def config_lock():
    with (ROOT / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def log(message):
    print(
        datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        message,
        flush=True,
    )


def api(path):
    result = subprocess.run(
        [GH, "api", path],
        env=ENV,
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
    return json.loads(result.stdout)


def eligible(config):
    repo, number = config["repo"], int(config["pr"])
    pr = api(f"repos/{repo}/pulls/{number}")
    if pr["state"] != "open" or pr["draft"]:
        return None, "PR is closed or draft"
    if (pr["head"].get("repo") or {}).get("full_name") != repo:
        return None, "Fork PRs are not allowed"
    sha = pr["head"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid commit")
    runs = api(
        f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&event=pull_request&per_page=20"
    )["workflow_runs"]
    runs = [
        r
        for r in runs
        if r["head_sha"] == sha and r["path"] == ".github/workflows/ci.yml"
    ]
    if not runs:
        return None, f"Waiting for CI: {sha[:12]}"
    run = max(runs, key=lambda r: r["id"])
    if run["status"] != "completed" or run["conclusion"] != "success":
        return None, f"CI {run['id']}: {run['status']} / {run['conclusion']}"
    jobs = api(
        f"repos/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
    )["jobs"]
    required = {"backend", "web", "compose-smoke"}
    if not required.issubset({j["name"] for j in jobs if j["conclusion"] == "success"}):
        return None, "Required CI jobs have not all succeeded"
    return {
        "sha": sha,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "pr": number,
    }, "CI passed"


def stage_source(sha, output):
    repository = str(ROOT / "source.git")
    git = ["/usr/bin/git", "--git-dir=" + repository, "-c", "core.hooksPath=/dev/null"]
    found = (
        subprocess.run(
            git + ["cat-file", "-e", sha + "^{commit}"],
            env=ENV,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if not found:
        subprocess.run(
            git + ["fetch", "--no-tags", "origin", sha],
            env=ENV,
            check=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
    release = shlex.quote(vm_path("releases/" + sha))
    command = f"mkdir -p {release} && tar xf - --no-same-owner -C {release} && touch {release}/.downloaded"
    producer = subprocess.Popen(
        git
        + [
            "archive",
            sha,
            "backend",
            "web",
            "docker",
            "docker-compose.yml",
            ".dockerignore",
        ],
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=output,
    )
    try:
        consumer = subprocess.run(
            [
                COLIMA,
                "ssh",
                "-p",
                settings()["profile"],
                "--",
                "sudo",
                "bash",
                "-c",
                command,
            ],
            env=ENV,
            check=False,
            stdin=producer.stdout,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        producer.stdout.close()
        if producer.wait(timeout=30) != 0 or consumer.returncode != 0:
            raise RuntimeError("Could not transfer exact-commit source")
    finally:
        if producer.poll() is None:
            producer.kill()
            producer.wait()


def vm_command(*args, output=None, check=True, timeout=180):
    return subprocess.run(
        [COLIMA, "ssh", "-p", settings()["profile"], "--", "sudo", *args],
        env=ENV,
        check=check,
        stdout=output or subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def ensure_vm():
    with (ROOT / "deploy.log").open("a") as output:
        subprocess.run(
            [
                COLIMA,
                "start",
                settings()["profile"],
                "--activate=false",
                "--mount",
                "none",
                "--ssh-agent=false",
                "--ssh-config=false",
            ],
            env=ENV,
            check=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=360,
        )


def phase(target, action):
    # Colima maps remote nonzero exits to 1. A per-call nonce preserves the busy
    # result without confusing an old result with an SSH/transport failure.
    nonce = uuid.uuid4().hex
    wrapper = 'bash "$1" "$2" "$3" "$4"; code=$?; printf "%s %s\\n" "$5" "$code" > "$6"; exit "$code"'
    with (ROOT / "deploy.log").open("a") as output:
        result = vm_command(
            "bash",
            "-c",
            wrapper,
            "--",
            vm_path("deploy.sh"),
            target["sha"],
            action,
            target.get("schema", ""),
            nonce,
            vm_path("phase-exit"),
            output=output,
            check=False,
            timeout=2400,
        )
    exit_record = (
        vm_command("cat", vm_path("phase-exit"), check=False).stdout.strip().split()
    )
    if exit_record == [nonce, "75"]:
        return False
    if result.returncode or exit_record != [nonce, "0"]:
        raise RuntimeError(f"{action} failed ({result.returncode}); see deploy.log")
    return True


def git(*args):
    return subprocess.check_output(
        [
            "/usr/bin/git",
            "--git-dir=" + str(ROOT / "source.git"),
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        env=ENV,
        timeout=120,
    ).decode()


def migration_files(sha):
    prefix = "backend/alembic/versions/"
    names = git("ls-tree", "-r", "--name-only", sha, prefix).splitlines()
    return {
        name: git("show", sha + ":" + name) for name in names if name.endswith(".py")
    }


def check_compatibility(previous, target):
    if not previous.get("sha"):
        raise RuntimeError(
            "Existing deployment must be adopted before automatic updates"
        )
    if git("merge-base", previous["sha"], target["sha"]).strip() != previous["sha"]:
        raise ValueError("History diverged; automatic downgrade is refused")
    current = vm_command(
        "docker",
        "exec",
        container("postgres"),
        "psql",
        "-U",
        "dlr",
        "-d",
        "dlr",
        "-Atc",
        "SELECT version_num FROM alembic_version",
    ).stdout.strip()
    expected = (
        previous.get("schema")
        or vm_command(
            "cat", vm_path(f"releases/{previous['sha']}/schema")
        ).stdout.strip()
    )
    if current != expected:
        raise ValueError(
            "Database revision differs from recorded deployment; inspect before updating"
        )
    target["schema"] = compatible(
        migration_files(previous["sha"]), migration_files(target["sha"]), current
    )


def transaction():
    result = vm_command("cat", vm_path("transaction.json"), check=False)
    if result.returncode:
        raise RuntimeError(
            "Cannot read deployment transaction; recovery requires inspection"
        )
    return json.loads(result.stdout)


def receipt(target):
    return json.loads(
        vm_command("cat", vm_path(f"releases/{target['sha']}/receipt.json")).stdout
    )


def healthy():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{settings()['web_port']}/api/health", timeout=5
        ) as response:
            body = json.load(response)
            return (
                response.status == 200
                and body.get("database") is True
                and body.get("rabbitmq", {}).get("ready") is True
                and preview_containers_running()
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def vm_running():
    result = subprocess.run(
        [COLIMA, "list", "--json"],
        env=ENV,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return any(
        item.get("name") == settings()["profile"] and item.get("status") == "Running"
        for item in (
            json.loads(line) for line in result.stdout.splitlines() if line.strip()
        )
    )


def preview_containers_running():
    result = subprocess.run(
        [
            COLIMA,
            "ssh",
            "-p",
            settings()["profile"],
            "--",
            "docker",
            "ps",
            "--format",
            "{{.Names}}",
        ],
        env=ENV,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return False
    running = set(result.stdout.splitlines())
    expected = {
        container("postgres"),
        container("rabbitmq"),
        container("control"),
        container("worker"),
        container("web"),
    }
    return expected.issubset(running)


def status(message, **fields):
    write(
        "status.json",
        {
            "checked_at": datetime.datetime.now().astimezone().isoformat(),
            "message": message,
            **fields,
        },
    )
    return message


def tick():
    config = read("config.json")
    if not config["enabled"]:
        return status("Paused; current preview remains available")
    previous = read("state.json", {})
    if read("attention.json"):
        return status(
            "Needs attention; inspect attention.json and VM transaction before retrying"
        )
    if previous.get("sha") and not healthy():
        if vm_running() and preview_containers_running():
            return status(
                "Needs attention: existing services retained after health failure"
            )
        ensure_vm()
        tx = transaction()
        if tx.get("phase") != "ready" or tx.get("sha") != previous["sha"]:
            write("attention.json", tx)
            return status("Unfinished deployment; automatic recovery refused")
        phase(previous, "recover")
        return status("Recovered verified local images", deployed=previous)
    target, reason = eligible(config)
    if target is None:
        return status(reason, deployed=previous)
    if previous.get("sha") == target["sha"]:
        return status("Ready", deployed=previous, candidate=target)
    write("candidate.json", target)
    ensure_vm()
    with (ROOT / "deploy.log").open("a") as output:
        stage_source(target["sha"], output)
    check_compatibility(previous, target)
    status(
        "Building candidate; current application remains available",
        candidate=target,
        deployed=previous,
    )
    phase(target, "stage")
    # Configuration changes may proceed during the build, but not during final switch.
    with config_lock():
        latest_config = read("config.json")
        if latest_config != config or not latest_config["enabled"]:
            return status(
                "Selection changed during build; candidate retained without switching"
            )
        latest, reason = eligible(latest_config)
        if latest is None or latest["sha"] != target["sha"]:
            return status("Candidate superseded or CI no longer successful: " + reason)
        target.update(latest)
        check_compatibility(previous, target)
        tx = transaction()
        if tx.get("phase") != "ready" or tx.get("sha") != previous["sha"]:
            write("attention.json", tx)
            return status("Unfinished deployment; inspect transaction before retrying")
        status("Switching after CI recheck", candidate=target, deployed=previous)
        # Persist before invoking the VM: process death cannot cause an automatic retry.
        write(
            "attention.json",
            {"phase": "switching", "candidate": target, "deployed": previous},
        )
        if not phase(target, "deploy"):
            (ROOT / "attention.json").unlink()
            return status(
                "Waiting for executions to become idle",
                candidate=target,
                deployed=previous,
            )
        target.update(receipt(target))
        target["deployed_at"] = datetime.datetime.now().astimezone().isoformat()
        write("state.json", target)
        (ROOT / "attention.json").unlink()
        return status("Ready", deployed=target)


def main():
    if not os.environ.get("DLR_PREVIEW_HOME"):
        sys.exit("Set DLR_PREVIEW_HOME to your private installation directory")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "watch",
            "once",
            "status",
            "select",
            "pause",
            "resume",
            "copy-token",
            "acknowledge",
        ],
    )
    parser.add_argument("pr", nargs="?", type=int)
    args = parser.parse_args()
    if args.command == "status":
        print(
            json.dumps(
                {
                    "config": read("config.json"),
                    "deployment": read("state.json"),
                    "status": read("status.json"),
                    "attention": read("attention.json"),
                    "candidate": read("candidate.json"),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if args.command in {"select", "pause", "resume", "acknowledge"}:
        with config_lock():
            config = read("config.json")
            if args.command == "select":
                if not args.pr or args.pr < 1:
                    parser.error("select requires a positive PR number")
                pr = api(f"repos/{config['repo']}/pulls/{args.pr}")
                if (
                    pr["state"] != "open"
                    or pr["draft"]
                    or (pr["head"].get("repo") or {}).get("full_name") != config["repo"]
                ):
                    parser.error(
                        "Select an open, non-draft PR in the configured repository"
                    )
                config["pr"] = args.pr
                config["enabled"] = True
            elif args.command == "acknowledge":
                tx = transaction()
                previous = read("state.json", {})
                if tx.get("phase") != "ready" or tx.get("sha") != previous.get("sha"):
                    parser.error(
                        "Reconcile VM transaction and deployed state first; database rollback is never automatic"
                    )
                (ROOT / "attention.json").unlink(missing_ok=True)
            else:
                config["enabled"] = args.command == "resume"
            write("config.json", config)
        print(json.dumps(config, indent=2))
        return
    if args.command == "copy-token":
        token = next(
            line.split("=", 1)[1]
            for line in (ROOT / "preview.env").read_text().splitlines()
            if line.startswith("DLR_ADMIN_TOKEN=")
        )
        subprocess.run(["/usr/bin/pbcopy"], input=token, text=True, check=True)
        print("Admin token copied to clipboard")
        return
    with (ROOT / "controller.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("Controller is already running")
        last = None
        while True:
            try:
                message = tick()
            except Exception as exc:
                message = f"Error: {type(exc).__name__}: {exc}"
                write(
                    "status.json",
                    {
                        "message": message,
                        "checked_at": datetime.datetime.now().astimezone().isoformat(),
                    },
                )
                if args.command == "once":
                    raise
            if message != last:
                log(message)
                last = message
            if args.command == "once":
                return
            time.sleep(max(30, int(read("config.json")["poll_seconds"])))


if __name__ == "__main__":
    main()
