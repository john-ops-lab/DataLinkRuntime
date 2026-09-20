#!/usr/bin/env python3
"""CI-gated local preview controller. PR code executes only in an unshared VM."""

import argparse
import base64
import contextlib
import datetime
import fcntl
import gc
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

import carry_forward
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

GROUP2_REQUIRED_JOBS = {"backend", "web", "compose-smoke", "local-preview"}
GROUP2_APPROVAL_HASHES = {
    "user_approval_sha256": "a595555cd4f39466a9477bef96c7bd6424dc878efa75883e6a8ff16de988749b",
    "product_scope_sha256": "53b52d4168f6ef7b7504b5eb6b229b5f784110c3bd4716ee0e447e6ec2f8bbc7",
    "review_bindings_sha256": "ba8dfb8e9d6fc043c049162ec56b35747edd84755789f1325b38b40492dfed3b",
}
GROUP2_HISTORICAL_REVIEWS = {
    "group2-product-integration-recheck.md":
        "e5c457872b19af0994ce76e3a894e8ffbb2cbd7a11868236d5c4bb8bd2fe152f",
    "group2-ci-web-code-review.md":
        "aa4a861dc2784244921a79af25183e4600b6328b8ddff573dbb3f1425c24c43f",
    "group2-python-harness-code-review.md":
        "31bc67be2eefadceba6c1d612731d24f349983adf61567386ebc56ea39062c2b",
}
_group2_install_context = None


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


def controller_files_digest():
    names = (
        "preview.py",
        "migrations.py",
        "deploy.sh",
        "verify.py",
        "assets.py",
        "carry_forward.py",
    )
    values = {}
    for name in names:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Trusted controller installation is incomplete")
        values[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return carry_forward.digest(values)


def migration_graph_digest(sha):
    return carry_forward.digest(migration_files(sha))


def private_directory(relative):
    path = ROOT / relative
    current = ROOT
    for part in Path(relative).parts:
        current = current / part
        current.mkdir(exist_ok=True, mode=0o700)
        if current.is_symlink() or not current.is_dir():
            raise ValueError("Private path is not a directory")
        os.chmod(current, 0o700)
    return path


def write_private_bytes(path, data):
    if not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
        raise ValueError("Private evidence parent must be mode 0700")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        os.chmod(temporary, 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private_json_stream(path, value):
    if not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
        raise ValueError("Private evidence parent must be mode 0700")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        temporary = Path(output.name)
        os.chmod(temporary, 0o600)
        for chunk in carry_forward.canonical_json_chunks(value):
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def vm_private_write(relative, data):
    if not re.fullmatch(r"[a-zA-Z0-9_./-]+", relative) or ".." in Path(relative).parts:
        raise ValueError("Invalid private VM path")
    destination = vm_path(relative)
    parent = str(Path(destination).parent)
    temporary = destination + ".tmp-" + uuid.uuid4().hex
    vm_command("install", "-d", "-m", "700", parent)
    result = subprocess.run(
        [
            COLIMA,
            "ssh",
            "-p",
            settings()["profile"],
            "--",
            "sudo",
            "tee",
            temporary,
        ],
        input=data,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=ENV,
        timeout=45,
    )
    if result.returncode:
        raise RuntimeError("Could not transfer private carry-forward evidence")
    vm_command("chmod", "600", temporary)
    vm_command("mv", temporary, destination)


def selected_manifest(config, previous, target):
    reference = config.get("carry_forward")
    if reference is None:
        return None, None
    if not isinstance(reference, dict) or set(reference) != {
        "manifest_id",
        "manifest_digest",
        "from_sha",
        "to_sha",
        "from_schema",
        "to_schema",
        "pr",
    }:
        raise RuntimeError("Invalid private carry-forward reference")
    manifest_id = reference["manifest_id"]
    if not isinstance(manifest_id, str) or not carry_forward.MANIFEST_ID.fullmatch(
        manifest_id
    ):
        raise RuntimeError("Invalid private carry-forward reference")
    path = ROOT / "carry-forward" / "manifests" / f"{manifest_id}.json"
    manifest = carry_forward.validate_manifest(carry_forward.read_private(path))
    expected_schema = (
        previous.get("schema")
        or vm_command(
            "cat", vm_path(f"releases/{previous['sha']}/schema")
        ).stdout.strip()
    )
    expected = {
        "manifest_id": manifest["manifest_id"],
        "manifest_digest": manifest["manifest_digest"],
        "from_sha": previous.get("sha"),
        "to_sha": target.get("sha"),
        "from_schema": expected_schema,
        "to_schema": target.get("schema"),
        "pr": target.get("pr"),
    }
    if reference != expected:
        raise RuntimeError("Carry-forward plan is not bound to this candidate")
    if manifest["repo"] != config["repo"] or any(
        manifest[key] != expected[key]
        for key in (
            "manifest_id",
            "manifest_digest",
            "from_sha",
            "to_sha",
            "from_schema",
            "to_schema",
            "pr",
        )
    ):
        raise RuntimeError("Carry-forward manifest binding changed")
    if manifest["controller_files_digest"] != controller_files_digest():
        raise RuntimeError("Carry-forward controller changed; create a new plan")
    if manifest["migration_graph_digest"] != migration_graph_digest(target["sha"]):
        raise RuntimeError("Carry-forward migration graph changed")
    if manifest.get("mode") == carry_forward.AUDITED_MODE and manifest[
        "source_diff"
    ] != audited_source_diff(manifest["from_sha"], manifest["to_sha"]):
        raise RuntimeError("Carry-forward source difference changed")
    if manifest.get("mode") == carry_forward.GROUP2_MODE:
        carry_forward.validate_group2_manifest_extensions(manifest)
        exact, reason = eligible(config, GROUP2_REQUIRED_JOBS)
        if exact is None or any(
            exact.get(key) != target.get(key)
            for key in ("sha", "run_id", "run_attempt", "pr")
        ):
            raise RuntimeError("Group2 exact CI binding changed: " + reason)
        target.update(exact)
        validate_group2_candidate_binding(manifest["review_scope"], exact)
        if manifest["source_diff"] != group2_source_diff(
            manifest["from_sha"], manifest["to_sha"], manifest["review_scope"]
        ):
            raise RuntimeError("Group2 source difference changed")
        stored_scope = (
            ROOT
            / "carry-forward"
            / "review-scopes"
            / manifest["review_scope_digest"]
            / "review-scope.json"
        )
        if (
            not stored_scope.exists()
            or carry_forward.read_private(stored_scope) != manifest["review_scope"]
        ):
            raise RuntimeError("Group2 reviewed scope evidence changed")
        validate_group2_artifacts(manifest["review_scope"], stored_scope)
        consumed = ROOT / "carry-forward" / "consumed" / path.name
        if consumed.exists():
            raise RuntimeError("Carry-forward manifest was already consumed")
    return manifest, path


@contextlib.contextmanager
def config_lock():
    with (ROOT / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        context = _group2_install_context
        if context is not None and context.get("operation_held"):
            validate_group2_install_locked(context)
        yield


@contextlib.contextmanager
def operation_lock(*, blocking=True):
    """Serialize one complete controller operation without owning the watcher."""
    with (ROOT / "operation.lock").open("a") as lock:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(lock, flags)
        context = _group2_install_context
        if context is not None:
            context["operation_held"] = True
        try:
            yield
        finally:
            if context is not None:
                context["operation_held"] = False


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


def eligible(config, required_jobs=None):
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
    extended_binding = required_jobs is not None
    required = required_jobs or {"backend", "web", "compose-smoke"}
    succeeded = {j["name"] for j in jobs if j["conclusion"] == "success"}
    if not required.issubset(succeeded):
        return None, "Required CI jobs have not all succeeded"
    target = {
        "sha": sha,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "pr": number,
    }
    if extended_binding:
        target.update(
            required_jobs=sorted(required),
            ci_binding={
                "head_sha": sha,
                "run_id": run["id"],
                "run_attempt": run["run_attempt"],
                "workflow_path": ".github/workflows/ci.yml",
                "event": "pull_request",
                "jobs": sorted(
                    (
                        {
                            "id": job["id"],
                            "name": job["name"],
                            "conclusion": job["conclusion"],
                        }
                        for job in jobs
                    ),
                    key=lambda item: (item["name"], item["id"]),
                ),
            },
        )
    return target, "CI passed"


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


def phase(target, action, manifest_id=""):
    # Colima maps remote nonzero exits to 1. A per-call nonce preserves the busy
    # result without confusing an old result with an SSH/transport failure.
    nonce = uuid.uuid4().hex
    wrapper = 'bash "$1" "$2" "$3" "$4" "$5" "$6"; code=$?; printf "%s %s\\n" "$7" "$code" > "$8"; exit "$code"'
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
            manifest_id,
            target.get("recovery_id", ""),
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


def plan_carry_forward(to_sha, ids_file, output, mode=None, review_scope=None):
    if not re.fullmatch(r"[0-9a-f]{40}", to_sha):
        raise ValueError("--to-sha must be a full lowercase commit SHA")
    if mode not in {None, carry_forward.AUDITED_MODE, carry_forward.GROUP2_MODE}:
        raise ValueError("Unsupported carry-forward mode")
    if (mode == carry_forward.GROUP2_MODE) != (review_scope is not None):
        raise ValueError("Group2 mode requires one reviewed scope; older modes forbid it")
    scope = (
        carry_forward.validate_group2_review_scope(
            carry_forward.read_private(review_scope)
        )
        if review_scope
        else None
    )
    if scope is not None:
        evidence = validate_group2_artifacts(scope, review_scope)
        stored = private_directory(
            f"carry-forward/review-scopes/{scope['scope_digest']}"
        )
        scope_destination = stored / "review-scope.json"
        if scope_destination.exists():
            if scope_destination.read_bytes() != carry_forward.canonical_bytes(scope) + b"\n":
                raise ValueError("Stored Group2 review scope changed")
        else:
            carry_forward.write_private(scope_destination, scope)
        evidence_destination = stored / "review-scope.evidence"
        private_directory(str(evidence_destination.relative_to(ROOT)))
        for source_path in evidence.rglob("*"):
            if source_path.is_file():
                relative = source_path.relative_to(evidence)
                destination = evidence_destination / relative
                private_directory(str(destination.parent.relative_to(ROOT)))
                if destination.exists() and destination.read_bytes() != source_path.read_bytes():
                    raise ValueError("Stored Group2 reviewed evidence changed")
                if not destination.exists():
                    write_private_bytes(destination, source_path.read_bytes())
    ids = carry_forward.normalize_selection(
        carry_forward.read_private(ids_file), mode=mode
    )
    config = read("config.json")
    previous = read("state.json", {})
    if read("attention.json"):
        raise RuntimeError("Resolve the existing deployment attention first")
    target, reason = eligible(
        config, GROUP2_REQUIRED_JOBS if scope is not None else None
    )
    if target is None or target["sha"] != to_sha:
        raise RuntimeError("Candidate is not the selected eligible HEAD: " + reason)
    candidate = read("candidate.json", {})
    if candidate.get("sha") != to_sha:
        raise RuntimeError("candidate_not_staged")
    ensure_vm()
    check_compatibility(previous, target)
    source_diff = (
        audited_source_diff(previous["sha"], target["sha"])
        if mode == carry_forward.AUDITED_MODE
        else None
    )
    images_result = vm_command(
        "cat", vm_path(f"releases/{to_sha}/images.json"), check=False
    )
    if images_result.returncode:
        raise RuntimeError("candidate_not_staged")
    candidate_images = json.loads(images_result.stdout)
    old_images = json.loads(
        vm_command("cat", vm_path(f"releases/{previous['sha']}/images.json")).stdout
    )
    if scope is not None:
        validate_group2_candidate_binding(scope, target)
        if scope["image_binding"]["candidate_image_ids"] != candidate_images or scope[
            "image_binding"
        ]["old_image_ids"] != old_images:
            raise RuntimeError("Group2 staged image binding changed")
    storage = []
    old_containers = []
    for service in ("postgres", "rabbitmq", "control", "worker"):
        container_id = vm_command(
            "docker", "inspect", container(service), "--format", "{{.Id}}"
        ).stdout.strip()
        image_id = vm_command(
            "docker", "inspect", container(service), "--format", "{{.Image}}"
        ).stdout.strip()
        labels = {
            "com.docker.compose.project": vm_command(
                "docker",
                "inspect",
                container(service),
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}',
            ).stdout.strip(),
            "com.docker.compose.service": vm_command(
                "docker",
                "inspect",
                container(service),
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}',
            ).stdout.strip(),
        }
        expected_images = [
            value
            for tag, value in old_images.items()
            if tag.endswith(f"-{service}:{previous['sha']}")
        ]
        if (
            (service != "rabbitmq" and len(expected_images) != 1)
            or (expected_images and image_id != expected_images[0])
            or labels
            != {
                "com.docker.compose.project": config["project"],
                "com.docker.compose.service": service,
            }
            or not re.fullmatch(r"[0-9a-f]{64}", container_id)
        ):
            raise RuntimeError("Old container identity is not closed")
        runtime_config = None
        if service == "worker":
            raw_config = json.loads(
                vm_command(
                    "docker",
                    "inspect",
                    container(service),
                    "--format",
                    "{{json .Config}}",
                ).stdout
            )
            try:
                runtime_config = carry_forward._worker_runtime_config(raw_config)
            except carry_forward.CarryForwardError as error:
                raise RuntimeError("Worker runtime config is not closed") from error
        old_containers.append(
            {
                "service": service,
                "container_id": container_id,
                "image_id": image_id,
                "labels": labels,
                **({"runtime_config": runtime_config} if runtime_config else {}),
            }
        )
        mounts = json.loads(
            vm_command(
                "docker",
                "inspect",
                container(service),
                "--format",
                "{{json .Mounts}}",
            ).stdout
        )
        for mount in mounts:
            mount_type = mount.get("Type")
            if mount_type not in {"volume", "bind", "tmpfs"}:
                raise RuntimeError("Old storage identity is not closed")
            storage.append(
                {
                    "service": service,
                    "type": mount_type,
                    "source": (
                        mount.get("Name")
                        if mount_type == "volume"
                        else mount.get("Source", "")
                        if mount_type == "bind"
                        else ""
                    ),
                    "destination": mount.get("Destination"),
                    "read_only": not bool(mount.get("RW")),
                }
            )
    worker_storage = [
        item
        for item in storage
        if item["service"] == "worker" and item["type"] == "volume"
    ]
    if (
        len({(item["service"], item["destination"]) for item in storage})
        != len(storage)
        or {item["destination"] for item in worker_storage}
        != {"/var/lib/dlr/runtime", "/var/lib/dlr/journal"}
        or any(
            not item["destination"] or (item["type"] != "tmpfs" and not item["source"])
            for item in storage
        )
    ):
        raise RuntimeError("Worker storage identity is not closed")
    try:
        storage = carry_forward.validate_storage_identity(storage)
    except carry_forward.CarryForwardError as error:
        raise RuntimeError("Worker storage identity is shadowed") from error
    manifest_id = uuid.uuid4().hex
    work_relative = f"carry-forward/work/{manifest_id}"
    context = {
        "manifest_id": manifest_id,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "repo": config["repo"],
        "pr": target["pr"],
        "from_sha": previous["sha"],
        "to_sha": target["sha"],
        "from_schema": previous.get("schema")
        or vm_command(
            "cat", vm_path(f"releases/{previous['sha']}/schema")
        ).stdout.strip(),
        "to_schema": target["schema"],
        "controller_files_digest": controller_files_digest(),
        "migration_graph_digest": migration_graph_digest(target["sha"]),
        "old_image_ids": old_images,
        "candidate_image_ids": candidate_images,
        "storage_identity": sorted(
            storage, key=lambda item: (item["service"], item["destination"])
        ),
        "old_containers": sorted(old_containers, key=lambda item: item["service"]),
    }
    if mode == carry_forward.AUDITED_MODE:
        context["mode"] = mode
        context["source_diff"] = source_diff
    elif mode == carry_forward.GROUP2_MODE:
        context.update(
            {
                "mode": mode,
                "source_diff": group2_source_diff(
                    previous["sha"], target["sha"], scope
                ),
                "review_scope": scope,
                "review_scope_digest": scope["scope_digest"],
                "ci_binding": scope["ci"],
                "preservation_reference_digest": scope[
                    "preservation_reference"
                ]["snapshot_digest"],
            }
        )
    vm_private_write(
        f"{work_relative}/ids.json", carry_forward.canonical_bytes(ids) + b"\n"
    )
    vm_private_write(
        f"{work_relative}/context.json", carry_forward.canonical_bytes(context) + b"\n"
    )
    phase(target, "plan", manifest_id)
    manifest = carry_forward.validate_manifest(
        json.loads(vm_command("cat", vm_path(f"{work_relative}/manifest.json")).stdout)
    )
    if scope is not None:
        carry_forward.validate_group2_manifest_extensions(manifest)
        if manifest["review_scope_digest"] != scope["scope_digest"]:
            raise RuntimeError("VM Group2 manifest changed the reviewed scope")
    carry_forward.write_private(output, manifest)
    return {
        "code": "manifest_ready",
        "manifest_id": manifest["manifest_id"],
        "manifest_digest": manifest["manifest_digest"],
        "from_sha": manifest["from_sha"],
        "to_sha": manifest["to_sha"],
        "selection_count": len(
            carry_forward.selected_execution_ids(manifest["selection"])
        ),
    }


def install_manifest(config, pr, source):
    manifest = carry_forward.validate_manifest(carry_forward.read_private(source))
    if manifest["repo"] != config["repo"] or manifest["pr"] != pr:
        raise ValueError("Carry-forward manifest belongs to another selection")
    state = read("state.json", {})
    if manifest["from_sha"] != state.get("sha"):
        raise ValueError("Carry-forward manifest does not start at the deployed SHA")
    if manifest["controller_files_digest"] != controller_files_digest():
        raise ValueError("Carry-forward manifest was created by another controller")
    if manifest["migration_graph_digest"] != migration_graph_digest(manifest["to_sha"]):
        raise ValueError("Carry-forward manifest migration graph changed")
    if manifest.get("mode") == carry_forward.AUDITED_MODE and manifest[
        "source_diff"
    ] != audited_source_diff(manifest["from_sha"], manifest["to_sha"]):
        raise ValueError("Carry-forward manifest source difference changed")
    if manifest.get("mode") == carry_forward.GROUP2_MODE:
        carry_forward.validate_group2_manifest_extensions(manifest)
        if state.get("history_anchor_sha", state.get("sha")) != state.get("sha"):
            raise ValueError("Group2 manifest requires the actual deployed source")
        target, reason = eligible(config | {"pr": pr}, GROUP2_REQUIRED_JOBS)
        if target is None:
            raise ValueError("Group2 exact CI is no longer eligible: " + reason)
        validate_group2_candidate_binding(manifest["review_scope"], target)
        stored_scope = (
            ROOT
            / "carry-forward"
            / "review-scopes"
            / manifest["review_scope_digest"]
            / "review-scope.json"
        )
        if (
            not stored_scope.exists()
            or carry_forward.read_private(stored_scope) != manifest["review_scope"]
        ):
            raise ValueError("Group2 reviewed scope evidence is not installed")
        validate_group2_artifacts(manifest["review_scope"], stored_scope)
        if manifest["ci_binding"] != manifest["review_scope"]["ci"]:
            raise ValueError("Group2 manifest CI binding changed")
    pull = api(f"repos/{config['repo']}/pulls/{pr}")
    if pull["head"]["sha"] != manifest["to_sha"]:
        raise ValueError("Carry-forward manifest is not bound to the current PR HEAD")
    directory = private_directory("carry-forward/manifests")
    destination = directory / f"{manifest['manifest_id']}.json"
    consumed = ROOT / "carry-forward" / "consumed" / destination.name
    if consumed.exists():
        raise ValueError("Carry-forward manifest was already consumed")
    if destination.exists():
        if destination.read_bytes() != carry_forward.canonical_bytes(manifest) + b"\n":
            raise ValueError("Active carry-forward manifest ID is already in use")
    else:
        carry_forward.write_private(destination, manifest)
    return {
        key: manifest[key]
        for key in (
            "manifest_id",
            "manifest_digest",
            "from_sha",
            "to_sha",
            "from_schema",
            "to_schema",
            "pr",
        )
    }


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


def git_bytes(*args):
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
    )


def _source_diff(from_sha, to_sha):
    for sha in (from_sha, to_sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid audited source commit")
        git("cat-file", "-e", sha + "^{commit}")
    raw = git_bytes(
        "diff-tree",
        "--raw",
        "-r",
        "-z",
        "--no-abbrev",
        "--no-renames",
        "--no-commit-id",
        from_sha,
        to_sha,
    )
    parts = raw.split(b"\0")
    if parts[-1] != b"":
        raise ValueError("Invalid Git raw difference")
    parts.pop()
    if len(parts) % 2:
        raise ValueError("Invalid Git raw difference")
    entries = []
    for index in range(0, len(parts), 2):
        try:
            header = parts[index].decode("ascii")
            path = parts[index + 1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Invalid Git raw difference") from error
        match = re.fullmatch(
            r":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([A-Z])",
            header,
        )
        if not match:
            raise ValueError("Invalid Git raw difference")
        old_mode, new_mode, old_oid, new_oid, status = match.groups()
        if status not in {"A", "M"} or old_oid == new_oid:
            raise ValueError("Source difference contains an unsupported change")
        if status == "M" and (
            old_mode != new_mode
            or git("cat-file", "-t", old_oid).strip() != "blob"
        ):
            raise ValueError("Source difference contains an unsupported modification")
        if status == "A" and (old_mode != "000000" or old_oid != "0" * 40):
            raise ValueError("Source difference contains an invalid addition")
        if new_mode not in {"100644", "100755"} or git(
            "cat-file", "-t", new_oid
        ).strip() != "blob":
            raise ValueError("Source difference is not a regular file")
        entries.append(
            {
                "status": status,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "old_oid": old_oid,
                "new_oid": new_oid,
                "path": path,
            }
        )
    entries.sort(key=lambda item: item["path"])
    if len({item["path"] for item in entries}) != len(entries):
        raise ValueError("Source difference contains duplicate paths")
    trees = {
        "from_sha": from_sha,
        "to_sha": to_sha,
        "from_tree": git("rev-parse", from_sha + "^{tree}").strip(),
        "to_tree": git("rev-parse", to_sha + "^{tree}").strip(),
        "raw_diff_sha256": hashlib.sha256(raw).hexdigest(),
        "entries": entries,
    }
    if not all(
        re.fullmatch(r"[0-9a-f]{40}", trees[key]) for key in ("from_tree", "to_tree")
    ):
        raise ValueError("Invalid source tree")
    trees["tree_digest"] = carry_forward.digest(
        {key: trees[key] for key in ("from_tree", "to_tree", "entries")}
    )
    return trees


def audited_source_diff(from_sha, to_sha):
    source = _source_diff(from_sha, to_sha)
    entries = source["entries"]
    for item in entries:
        expected_mode = carry_forward.AUDITED_SOURCE_MODES.get(item["path"])
        if (
            item["status"] != "M"
            or expected_mode is None
            or item["old_mode"] != expected_mode
            or item["new_mode"] != expected_mode
        ):
            raise ValueError("Audited source difference is outside the approved scope")
    if not entries or "web/src/index.css" not in {item["path"] for item in entries}:
        raise ValueError("Audited source difference is missing the approved Web fix")
    return {"tree_digest": source["tree_digest"], "entries": entries}


def group2_source_diff(from_sha, to_sha, scope):
    source = _source_diff(from_sha, to_sha)
    return carry_forward.validate_group2_source_diff(source, scope)


def validate_group2_artifacts(scope, scope_path):
    directory = scope_path.parent / (scope_path.stem + ".evidence")
    expected = {
        "approval/REQUEST-ready.md": scope["approval"]["request_sha256"],
        "approval/USER-APPROVAL.json": scope["approval"]["user_approval_sha256"],
        "approval/product-scope.json": scope["approval"]["product_scope_sha256"],
        "approval/review-bindings.json": scope["approval"]["review_bindings_sha256"],
        "ci/" + scope["ci"]["evidence_sha256"]: scope["ci"]["evidence_sha256"],
        "preservation/" + scope["preservation_reference"]["review_report_sha256"]:
            scope["preservation_reference"]["review_report_sha256"],
    }
    expected.update(
        {
            "reviews/" + review["report_sha256"]: review["report_sha256"]
            for review in scope["reviews"]
        }
    )
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Group2 reviewed evidence directory is missing")
    for path in (directory, *(item for item in directory.rglob("*") if item.is_dir())):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Group2 reviewed evidence directory is not private")
    actual = {
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
    }
    if actual != set(expected):
        raise ValueError("Group2 reviewed evidence set changed")
    for relative, digest in expected.items():
        path = directory / relative
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o177 != 0
        ):
            raise ValueError("Group2 reviewed evidence is not a private regular file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Group2 reviewed evidence digest changed")
    approval = directory / "approval"
    if any(
        scope["approval"][key] != expected_digest
        for key, expected_digest in GROUP2_APPROVAL_HASHES.items()
    ):
        raise ValueError("Group2 historical approval anchor changed")
    try:
        user_approval = json.loads((approval / "USER-APPROVAL.json").read_text())
        product_scope = json.loads((approval / "product-scope.json").read_text())
        review_bindings = json.loads((approval / "review-bindings.json").read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Group2 approval evidence is invalid") from error
    if (
        user_approval.get("status") != "USER_APPROVED"
        or user_approval.get("user_reply") != "批准"
        or user_approval.get("request_sha256")
        != scope["approval"]["request_sha256"]
        or user_approval.get("product_candidate_sha")
        != scope["product_anchor"]["head_sha"]
        or {key: product_scope.get(key) for key in scope["product_anchor"]}
        != scope["product_anchor"]
    ):
        raise ValueError("Group2 approval evidence contradicts the reviewed scope")
    historical = review_bindings.get("reviews_sha256")
    commits = {
        "group2-product-integration-recheck.md": review_bindings.get(
            "integration_review_bound_commit"
        ),
        "group2-ci-web-code-review.md": review_bindings.get(
            "ci_test_review_bound_commit"
        ),
        "group2-python-harness-code-review.md": review_bindings.get(
            "harness_review_bound_commit"
        ),
    }
    if (
        not isinstance(historical, dict)
        or historical != GROUP2_HISTORICAL_REVIEWS
        or not all(isinstance(value, str) for value in commits.values())
    ):
        raise ValueError("Group2 inherited review bindings changed")

    def machine_records(path):
        try:
            text_value = path.read_text()
        except (OSError, UnicodeError) as error:
            raise ValueError("Group2 review report is unreadable") from error
        records = []
        for body in re.findall(r"```json\s*\n(.*?)\n```", text_value, re.DOTALL):
            try:
                value = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    final_sha = scope["final_source"]["to_sha"]
    for review in scope["reviews"]:
        report_path = directory / "reviews" / review["report_sha256"]
        if review["name"] in historical:
            if (
                review["report_sha256"] != historical[review["name"]]
                or review["reviewed_commit"] != commits[review["name"]]
            ):
                raise ValueError("Group2 inherited review identity changed")
        else:
            matches = [
                item
                for item in machine_records(report_path)
                if item.get("schema") == "group2-independent-review-v1"
            ]
            if len(matches) != 1:
                raise ValueError("Group2 independent review is not machine approved")
            approved = matches[0]
            if (
                approved.get("status") != "APPROVED"
                or approved.get("reviewed_commit") != review["reviewed_commit"]
                or approved.get("source_kind") != "git_commit"
                or approved.get("blocking_findings") != []
                or set(approved)
                != {
                    "schema",
                    "status",
                    "reviewed_commit",
                    "source_kind",
                    "coverage",
                    "blocking_findings",
                }
            ):
                raise ValueError("Group2 independent review is not machine approved")
            if review["reviewed_commit"] != final_sha:
                raise ValueError("Group2 independent review is not bound to final source")
            reported_items = approved.get("coverage")
            if not isinstance(reported_items, list):
                raise ValueError("Group2 independent review coverage changed")
            reported = {item.get("path"): item for item in reported_items if isinstance(item, dict)}
            if (
                len(reported_items) != len(reported)
                or set(reported) != {item["path"] for item in review["coverage"]}
            ):
                raise ValueError("Group2 independent review coverage changed")
            for item in review["coverage"]:
                actual = reported[item["path"]]
                line = git("ls-tree", final_sha, "--", item["path"]).strip()
                match = re.fullmatch(
                    r"(100644|100755) blob ([0-9a-f]{40})\t"
                    + re.escape(item["path"]),
                    line,
                )
                content_sha256 = hashlib.sha256(
                    git_bytes("show", final_sha + ":" + item["path"])
                ).hexdigest()
                if (
                    set(actual) != {"path", "mode", "blob_oid", "sha256"}
                    or match is None
                    or actual
                    != {
                        "path": item["path"],
                        "mode": match.group(1),
                        "blob_oid": item["blob_oid"],
                        "sha256": content_sha256,
                    }
                ):
                    raise ValueError("Group2 independent review bytes changed")
        for item in review["coverage"]:
            path = item["path"]
            for sha in (review["reviewed_commit"], final_sha):
                line = git("ls-tree", sha, "--", path).strip()
                match = re.fullmatch(
                    r"(100644|100755) blob ([0-9a-f]{40})\t" + re.escape(path),
                    line,
                )
                if match is None or match.group(2) != item["blob_oid"]:
                    raise ValueError("Group2 review no longer covers the final blob")
    try:
        ci_evidence = json.loads(
            (directory / "ci" / scope["ci"]["evidence_sha256"]).read_text()
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Group2 CI evidence is invalid") from error
    run = ci_evidence.get("run")
    jobs_response = ci_evidence.get("jobs")
    if not isinstance(run, dict) or not isinstance(jobs_response, dict):
        raise ValueError("Group2 CI evidence is not a raw GitHub API record")
    jobs = jobs_response.get("jobs")
    if (
        not isinstance(jobs, list)
        or jobs_response.get("total_count") != len(jobs)
        or len(jobs) != len({job.get("id") for job in jobs if isinstance(job, dict)})
    ):
        raise ValueError("Group2 CI jobs evidence is incomplete")
    normalized_jobs = [
        {key: job.get(key) for key in ("id", "name", "conclusion")}
        for job in jobs
        if isinstance(job, dict)
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and job.get("run_id") == run.get("id")
        and job.get("run_attempt") == run.get("run_attempt")
        and job.get("head_sha") == run.get("head_sha")
    ]
    normalized_jobs.sort(key=lambda item: (item["name"], item["id"]))
    normalized_ci = {
        "head_sha": run.get("head_sha"),
        "run_id": run.get("id"),
        "run_attempt": run.get("run_attempt"),
        "workflow_path": run.get("path"),
        "event": run.get("event"),
        "jobs": normalized_jobs,
    }
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("Group2 CI run is not successful")
    if normalized_ci != {
        key: scope["ci"][key]
        for key in (
            "head_sha",
            "run_id",
            "run_attempt",
            "workflow_path",
            "event",
            "jobs",
        )
    }:
        raise ValueError("Group2 CI evidence contradicts the reviewed scope")
    preservation_path = (
        directory
        / "preservation"
        / scope["preservation_reference"]["review_report_sha256"]
    )
    preservation_records = [
        item
        for item in machine_records(preservation_path)
        if item.get("schema") == "group2-preservation-review-v1"
    ]
    if len(preservation_records) != 1:
        raise ValueError("Group2 preservation review is not machine approved")
    preservation = preservation_records[0]
    source_kind = preservation.get("source_kind")
    chain_records = [
        item
        for item in machine_records(preservation_path)
        if item.get("schema") == "group2-reconcile-chain-v1"
    ]
    partial_chain_records = [
        item
        for item in machine_records(preservation_path)
        if item.get("schema") == "group2-partial-finalize-chain-v1"
    ]
    if source_kind == "private_snapshot":
        valid_source = not chain_records and not partial_chain_records
    elif source_kind == "group2_starting_reconcile_v1":
        if len(chain_records) != 1 or partial_chain_records:
            raise ValueError("Group2 reconciliation chain is missing or ambiguous")
        try:
            snapshot = carry_forward.validate_group2_reconcile_preservation(
                chain_records[0],
                chain_records[0]["failed_manifest"]["review_scope"][
                    "preservation_reference"
                ],
            )
        except (KeyError, carry_forward.CarryForwardError) as error:
            raise ValueError("Group2 reconciliation chain is invalid") from error
        valid_source = snapshot == scope["preservation_reference"]["snapshot"]
    elif source_kind == "group2_partial_finalize_v1":
        if len(partial_chain_records) != 1 or chain_records:
            raise ValueError("Group2 partial finalize chain is missing or ambiguous")
        chain = partial_chain_records[0]
        try:
            original_reference = (
                carry_forward.group2_partial_finalize_original_reference(chain)
            )
            snapshot = carry_forward.validate_group2_partial_finalize_preservation(
                chain, original_reference
            )
        except (KeyError, carry_forward.CarryForwardError) as error:
            raise ValueError("Group2 partial finalize chain is invalid") from error
        valid_source = _partial_finalize_scope_binding(chain, scope, snapshot)
    else:
        valid_source = False
    if not (
        valid_source
        and preservation.get("status") == "APPROVED"
        and preservation.get("snapshot_digest")
        == scope["preservation_reference"]["snapshot_digest"]
        and preservation.get("lineage")
        == scope["preservation_reference"]["snapshot"]["lineage"]
        and set(preservation)
        == {"schema", "status", "source_kind", "snapshot_digest", "lineage"}
    ):
        raise ValueError("Group2 preservation review is not machine approved")
    return directory


def _partial_finalize_scope_binding(chain, scope, snapshot):
    tool = chain.get("request", {}).get("tool", {})
    return (
        snapshot == scope["preservation_reference"]["snapshot"]
        and tool.get("sha") == scope["final_source"]["to_sha"]
        and tool.get("controller_files") == scope["controller_files"]["files"]
    )


def validate_group2_candidate_binding(scope, target, stage=None):
    scope = carry_forward.validate_group2_review_scope(scope)
    final = scope["final_source"]
    anchor = scope["product_anchor"]
    if target.get("sha") != final["to_sha"] or target.get("pr") != scope["pr"]:
        raise ValueError("Group2 candidate does not match the reviewed final source")
    if git("merge-base", final["from_sha"], anchor["head_sha"]).strip() != final[
        "from_sha"
    ] or git("merge-base", anchor["head_sha"], final["to_sha"]).strip() != anchor[
        "head_sha"
    ]:
        raise ValueError("Group2 reviewed ancestry changed")
    source = group2_source_diff(final["from_sha"], final["to_sha"], scope)
    if source != final:
        raise ValueError("Group2 final source difference changed")
    controller_paths = {
        name: "tools/local-preview/" + name
        for name in (
            "preview.py",
            "migrations.py",
            "deploy.sh",
            "verify.py",
            "assets.py",
            "carry_forward.py",
        )
    }
    for name, path in controller_paths.items():
        actual = hashlib.sha256(
            git_bytes("show", final["to_sha"] + ":" + path)
        ).hexdigest()
        if actual != scope["controller_files"]["files"][name]:
            raise ValueError("Group2 reviewed controller bytes changed")
    ci = scope["ci"]
    actual_ci = target.get("ci_binding", {})
    if any(actual_ci.get(key) != ci[key] for key in (
        "head_sha", "run_id", "run_attempt", "workflow_path", "event", "jobs"
    )) or target.get("required_jobs") != sorted(GROUP2_REQUIRED_JOBS):
        raise ValueError("Group2 exact CI binding changed")
    migration = scope["migration_graph"]
    if (
        migration_graph_digest(final["from_sha"]) != migration["graph_digest"]
        or migration_graph_digest(final["to_sha"]) != migration["graph_digest"]
        or migration_inventory(final["from_sha"]) != migration["from_files"]
        or migration_inventory(final["to_sha"]) != migration["to_files"]
        or compatible(
            migration_files(final["from_sha"]),
            migration_files(final["to_sha"]),
            migration["head"],
        )
        != migration["head"]
    ):
        raise ValueError("Group2 migration graph changed")
    if stage is not None and stage != scope["image_binding"]:
        raise ValueError("Group2 staged images changed")
    return scope


def validate_group2_install_locked(context):
    """Fail closed inside install.py's operation -> config critical section."""
    scope_path = Path(context["scope_path"])
    scope = carry_forward.validate_group2_review_scope(
        carry_forward.read_private(scope_path)
    )
    if scope != context["scope"]:
        raise RuntimeError("Group2 reviewed scope changed before installation")
    validate_group2_artifacts(scope, scope_path)
    source = Path(context["source"])
    head = subprocess.check_output(
        ["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"],
        env=ENV,
        text=True,
        timeout=30,
    ).strip()
    dirty = subprocess.check_output(
        ["/usr/bin/git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
        env=ENV,
        text=True,
        timeout=30,
    )
    if head != scope["final_source"]["to_sha"] or dirty:
        raise RuntimeError("Reviewed Group2 controller source checkout changed")
    expected_files = scope["controller_files"]["files"]
    for name, expected in expected_files.items():
        path = source / "tools" / "local-preview" / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Reviewed Group2 controller source is incomplete")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Reviewed Group2 controller source bytes changed")
    config = read("config.json")
    previous = read("state.json", {})
    if previous.get("sha") != scope["final_source"]["from_sha"]:
        raise RuntimeError("Group2 installation no longer starts at the approved deployment")
    target, reason = eligible(config, GROUP2_REQUIRED_JOBS)
    if target is None:
        raise RuntimeError("Group2 reviewed CI is no longer eligible: " + reason)
    images = json.loads(
        vm_command(
            "cat", vm_path(f"releases/{target['sha']}/images.json")
        ).stdout
    )
    old_images = json.loads(
        vm_command(
            "cat", vm_path(f"releases/{previous['sha']}/images.json")
        ).stdout
    )
    if scope["image_binding"] != {
        "old_image_ids": old_images,
        "candidate_image_ids": images,
    }:
        raise RuntimeError("Group2 reviewed staged images changed")
    validate_group2_candidate_binding(scope, target, scope["image_binding"])
    context["locked_validations"] = context.get("locked_validations", 0) + 1


def verify_group2_installation(scope):
    files = scope["controller_files"]["files"]
    for name, expected in files.items():
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Installed Group2 controller is incomplete")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Installed Group2 controller bytes changed")
    for name in ("deploy.sh", "verify.py", "assets.py", "carry_forward.py"):
        actual = vm_command("sha256sum", vm_path(name)).stdout.split()[0]
        if actual != files[name]:
            raise RuntimeError("Installed Group2 VM controller bytes changed")


def install_group2(review_scope):
    global _group2_install_context
    if _group2_install_context is not None:
        raise RuntimeError("Group2 installation context is already active")
    scope = carry_forward.validate_group2_review_scope(
        carry_forward.read_private(review_scope)
    )
    validate_group2_artifacts(scope, review_scope)
    if read("config.json", {}).get("enabled") is not False:
        raise RuntimeError("Pause the watcher before installing the Group2 controller")
    source = Path(__file__).resolve().parents[2]
    context = {
        "scope": scope,
        "scope_path": str(review_scope.resolve()),
        "source": str(source),
        "operation_held": False,
        "locked_validations": 0,
    }
    _group2_install_context = context
    original_argv = sys.argv
    original_preview_module = sys.modules.get("preview")
    try:
        # install.py imports preview by name. Reuse this exact module so its real
        # second config_lock sees the in-process review context.
        sys.modules["preview"] = sys.modules[__name__]
        import install

        sys.argv = [str(Path(install.__file__).resolve())]
        install.main()
        if context["locked_validations"] != 1:
            raise RuntimeError("Official installer did not execute the Group2 lock guard")
        if read("config.json", {}).get("enabled") is not False:
            raise RuntimeError("Group2 controller must remain paused after installation")
        verify_group2_installation(scope)
    finally:
        sys.argv = original_argv
        if original_preview_module is None:
            sys.modules.pop("preview", None)
        else:
            sys.modules["preview"] = original_preview_module
        _group2_install_context = None


def migration_files(sha):
    prefix = "backend/alembic/versions/"
    names = git("ls-tree", "-r", "--name-only", sha, prefix).splitlines()
    return {
        name: git("show", sha + ":" + name) for name in names if name.endswith(".py")
    }


def migration_inventory(sha):
    prefix = "backend/alembic/versions/"
    output = git("ls-tree", "-r", sha, prefix).splitlines()
    inventory = []
    for line in output:
        match = re.fullmatch(r"([0-7]{6}) blob ([0-9a-f]{40})\t(.+)", line)
        if not match or not match.group(3).endswith(".py"):
            raise ValueError("Invalid migration inventory")
        inventory.append(
            {"path": match.group(3), "mode": match.group(1), "oid": match.group(2)}
        )
    return sorted(inventory, key=lambda item: item["path"])


def check_compatibility(previous, target):
    if not previous.get("sha"):
        raise RuntimeError(
            "Existing deployment must be adopted before automatic updates"
        )
    anchor = previous.get("history_anchor_sha", previous["sha"])
    if not re.fullmatch(r"[0-9a-f]{40}", anchor):
        raise ValueError("Invalid manually reconciled history anchor")
    if anchor != previous["sha"]:
        # A private, manually recorded anchor can reconcile rewritten documentation
        # history only when every deployed source path is exactly unchanged.
        git(
            "diff",
            "--exit-code",
            previous["sha"],
            anchor,
            "--",
            "backend",
            "web",
            "docker",
            "docker-compose.yml",
            ".dockerignore",
        )
    if git("merge-base", anchor, target["sha"]).strip() != anchor:
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


def group2_receipt_evidence(value, manifest):
    base = f"carry-forward/check/{manifest['manifest_id']}"

    def vm_json(relative):
        return json.loads(vm_command("cat", vm_path(f"{base}/{relative}")).stdout)

    backup = value.get("backup")
    expected_prefix = vm_path("backups/")
    if not isinstance(backup, str) or not backup.startswith(expected_prefix):
        raise RuntimeError("Invalid Group2 backup receipt path")
    dump_hash = vm_command("sha256sum", backup + "/database.dump").stdout.split()[0]
    list_hash = vm_command("sha256sum", backup + "/database.list").stdout.split()[0]
    stages = {
        "preflight": vm_json("preflight/db.json"),
        "control_stopped": vm_json("control-stopped/db.json"),
        "stopped": vm_json("stopped/db.json"),
        "backup": {
            "db": vm_json("after-backup/db.json"),
            "dump_sha256": dump_hash,
            "list_sha256": list_hash,
        },
        "same_schema": vm_json("after-migration/db.json"),
        "started": vm_json("group2/started-check.json"),
        "probe": vm_json("group2/probe-final.json")["probe_proof"][
            "probe_result"
        ],
        "natural_cleanup": vm_json("group2/cleanup.json")["cleanup"],
        "post_preservation": {
            "result": vm_json("group2/post-preservation.json"),
            "db": vm_json("group2/final/db.json"),
            "files": vm_json("group2/final/files.json"),
        },
        "post_health": {
            "account_check": vm_json("group2/account-after.json")["account_check"],
            "entry_probe": vm_json("group2/entry-after.json")["entry_probe"],
            "logs_after": vm_json("group2/log-after-health.json")["log_evidence"],
        },
    }
    stage_inputs = {
        name: {
            "db": vm_json(f"{path}/db.json"),
            "files": vm_json(f"{path}/files.json"),
            "logs_after": vm_json(f"{path}/log.json")["log_evidence"],
        }
        for name, path in {
            "preflight": "preflight",
            "control_stopped": "control-stopped",
            "stopped": "stopped",
            "after_backup": "after-backup",
            "after_migration": "after-migration",
        }.items()
    }
    startup_request = vm_json("group2/startup-request.json")
    startup = {
        "request": startup_request,
        "proof": vm_json("group2/startup.json")["startup_proof"],
        "before_files": manifest["file_evidence"],
        "after_files": vm_json("group2/started/files.json"),
        "after_db": vm_json("group2/started/db.json"),
        "result": vm_json("group2/started-check.json"),
    }
    cleanup = vm_json("group2/cleanup.json")["cleanup"]
    probe = {
        "proof": vm_json("group2/probe-final.json")["probe_proof"],
        "before_db": vm_json("group2/started/db.json"),
        "after_db": vm_json("group2/final/db.json"),
        "before_files": vm_json("group2/started/files.json"),
        "after_files": vm_json("group2/final/files.json"),
        "logs_before": vm_json("group2/log-before-probe.json")["log_evidence"],
        "logs_partial": vm_json("group2/log-partial.json")["log_evidence"],
        "logs_final": vm_json("group2/log-final.json")["log_evidence"],
        "logs_complete": vm_json("group2/log-complete.json")["log_evidence"],
        "probe_result": stages["probe"],
        "cleanup": cleanup,
        "result": vm_json("group2/post-preservation.json"),
    }
    account = vm_json("group2/account-after.json")["account_check"]
    entry = vm_json("group2/entry-after.json")["entry_probe"]
    post_health = {
        "account_check": account,
        "entry_probe": entry,
        "logs_before": vm_json("group2/log-before-health.json")["log_evidence"],
        "logs_after": vm_json("group2/log-after-health.json")["log_evidence"],
    }
    return {
        "stages": stages,
        "stage_inputs": stage_inputs,
        "startup": startup,
        "probe": probe,
        "post_health": post_health,
    }


def validate_group2_vm_commit(manifest):
    current = vm_command("cat", vm_path("current-sha")).stdout.strip()
    tx = transaction()
    reference = tx.get("carry_forward")
    if (
        current != manifest["to_sha"]
        or tx.get("phase") != "ready"
        or tx.get("sha") != manifest["to_sha"]
        or not isinstance(reference, dict)
        or reference.get("manifest_id") != manifest["manifest_id"]
        or reference.get("manifest_digest") != manifest["manifest_digest"]
        or reference.get("from_sha") != manifest["from_sha"]
        or reference.get("to_sha") != manifest["to_sha"]
    ):
        raise RuntimeError("Group2 VM commit binding changed")


def validate_group2_receipt(value, target, manifest, evidence):
    required = {
        "mode",
        "sha",
        "schema",
        "images",
        "probe",
        "backup",
        "carry_forward",
        "ci_binding",
        "review_scope_digest",
        "stages",
        "post_preservation_digest",
        "evidence_digest",
        "account_entry_digest",
        "account_entry",
        "account_ready",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("Invalid Group2 deployment receipt")
    carry = value["carry_forward"]
    try:
        account_entry = carry_forward.validate_group2_account_entry(
            value["account_entry"]
        )
    except carry_forward.CarryForwardError as error:
        raise RuntimeError("Invalid Group2 account entry receipt") from error
    if (
        value["mode"] != carry_forward.GROUP2_MODE
        or value["sha"] != target["sha"]
        or value["schema"] != target["schema"]
        or value["images"] != manifest["candidate_image_ids"]
        or value["ci_binding"] != manifest["ci_binding"]
        or value["review_scope_digest"] != manifest["review_scope_digest"]
        or carry.get("manifest_id") != manifest["manifest_id"]
        or carry.get("manifest_digest") != manifest["manifest_digest"]
        or value["account_ready"] is not True
        or value["account_entry"] != manifest["account_entry"]
        or account_entry["profile_digest"] != value["account_entry_digest"]
    ):
        raise RuntimeError("Group2 deployment receipt binding changed")
    required_stages = {
        "preflight",
        "control_stopped",
        "stopped",
        "backup",
        "same_schema",
        "started",
        "probe",
        "natural_cleanup",
        "post_preservation",
        "post_health",
    }
    if set(value["stages"]) != required_stages or not all(
        isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
        for item in value["stages"].values()
    ):
        raise RuntimeError("Group2 deployment receipt is missing a preservation stage")
    for key in (
        "post_preservation_digest",
        "evidence_digest",
        "account_entry_digest",
    ):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise RuntimeError("Invalid Group2 deployment receipt digest")
    if not isinstance(evidence, dict) or set(evidence) != {
        "stages",
        "stage_inputs",
        "startup",
        "probe",
        "post_health",
    }:
        raise RuntimeError("Group2 receipt evidence set changed")
    stages = evidence["stages"]
    if not isinstance(stages, dict) or set(stages) != required_stages:
        raise RuntimeError("Group2 receipt evidence set changed")
    expected_stages = {
        key: carry_forward.digest(stages[key]) for key in required_stages
    }
    if value["stages"] != expected_stages:
        raise RuntimeError("Group2 receipt evidence digest changed")
    if (
        value["post_preservation_digest"]
        != expected_stages["post_preservation"]
        or stages["probe"] != value["probe"]
    ):
        raise RuntimeError("Group2 receipt preservation binding changed")
    probe = value["probe"]
    if (
        not isinstance(probe, dict)
        or set(probe) != {"status", "workspace_cleanup_status", "execution_id"}
        or probe.get("status") != "succeeded"
        or probe.get("workspace_cleanup_status") != "completed"
        or not isinstance(probe.get("execution_id"), int)
        or isinstance(probe.get("execution_id"), bool)
        or probe["execution_id"] < 1
    ):
        raise RuntimeError("Group2 official probe did not complete")
    try:
        validated_evidence = carry_forward.validate_group2_receipt_evidence(
            manifest, evidence
        )
    except carry_forward.CarryForwardError as error:
        raise RuntimeError("Group2 receipt evidence is not valid") from error
    if not isinstance(validated_evidence, dict):
        raise RuntimeError("Group2 receipt evidence is not valid")
    if value["evidence_digest"] != carry_forward.digest(evidence):
        raise RuntimeError("Group2 receipt raw evidence binding changed")
    return {
        "mode": value["mode"],
        "schema": value["schema"],
        "images": value["images"],
        "probe_succeeded": True,
        "carry_forward": {
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["manifest_digest"],
            "review_scope_digest": manifest["review_scope_digest"],
            "post_preservation_digest": value["post_preservation_digest"],
            "account_entry_digest": value["account_entry_digest"],
            "account_ready": True,
        },
    }


def consume_manifest(manifest_path):
    consumed = private_directory("carry-forward/consumed") / manifest_path.name
    if consumed.exists():
        raise RuntimeError("Carry-forward manifest was already consumed")
    os.link(manifest_path, consumed)
    try:
        os.unlink(manifest_path)
        for directory in (consumed.parent, manifest_path.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        # Keep the immutable consumed copy. A remaining active link is attention,
        # never permission to overwrite or run the probe again.
        raise


def validate_group2_recovery_evidence(manifest, recovery_id, expected_deployment):
    cache = {}
    visiting = set()

    def validate_node(identity):
        if identity in cache:
            return cache[identity]
        if identity in visiting:
            raise RuntimeError("Group2 recovery predecessor cycle")
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"[0-9a-f]{32}", identity) is None
        ):
            raise RuntimeError("Invalid Group2 recovery identity")
        visiting.add(identity)
        try:
            node = load_node(identity)
        finally:
            visiting.remove(identity)
        cache[identity] = node
        return node

    def load_node(identity):
        base = f"carry-forward/recovery-{manifest['to_sha']}-{identity}"

        def vm_json(relative):
            return json.loads(vm_command("cat", vm_path(f"{base}/{relative}")).stdout)

        baseline = vm_json("baseline.json")
        evidence = {
            "request": vm_json("startup-request.json"),
            "proof": vm_json("startup.json")["startup_proof"],
            "before_db": vm_json("before-db.json"),
            "after_db": vm_json("after-db.json"),
            "before_files": vm_json("before-files.json"),
            "after_files": vm_json("after-files.json"),
            "account_check": vm_json("account.json")["account_check"],
            "entry_probe": vm_json("entry.json")["entry_probe"],
            "logs_before": vm_json("log-before.json")["log_evidence"],
            "logs_after": vm_json("log-after.json")["log_evidence"],
            "preservation": vm_json("preservation.json"),
        }
        completion = vm_json("completion.json")
        if (
            not isinstance(baseline, dict)
            or baseline.get("deployment") != expected_deployment
        ):
            raise RuntimeError("Group2 recovery deployment root changed")
        predecessor = baseline.get("predecessor")
        if not isinstance(predecessor, dict):
            raise RuntimeError("Group2 recovery predecessor binding changed")
        if predecessor.get("kind") == "deployment":
            expected_predecessor = {
                "kind": "deployment",
                "recovery_id": None,
                "evidence_digest": None,
                "db": expected_deployment["db"],
                "files": expected_deployment["files"],
                "logs_after": expected_deployment["logs_after"],
            }
        elif predecessor.get("kind") == "recovery":
            prior = validate_node(predecessor.get("recovery_id"))
            expected_predecessor = {
                "kind": "recovery",
                "recovery_id": prior["recovery_id"],
                "evidence_digest": prior["evidence_digest"],
                "db": prior["after_db"],
                "files": prior["after_files"],
                "logs_after": prior["logs_after"],
            }
        else:
            raise RuntimeError("Group2 recovery predecessor binding changed")
        if predecessor != expected_predecessor:
            raise RuntimeError("Group2 recovery predecessor evidence changed")
        evidence_digest = carry_forward.digest(
            {"baseline": baseline, "evidence": evidence}
        )
        try:
            result = carry_forward.validate_group2_recovery_evidence(
                manifest, baseline, evidence, expected_deployment
            )
        except carry_forward.CarryForwardError as error:
            raise RuntimeError("Group2 recovery evidence is not valid") from error
        expected_completion = {
            "schema": "group2-recovery-completion-v1",
            "recovery_id": identity,
            "sha": manifest["to_sha"],
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["manifest_digest"],
            "evidence_digest": evidence_digest,
            "predecessor": {
                key: expected_predecessor[key]
                for key in ("kind", "recovery_id", "evidence_digest")
            },
            "result": result,
        }
        if completion != expected_completion:
            raise RuntimeError("Group2 recovery completion binding changed")
        return {
            "recovery_id": identity,
            "evidence_digest": evidence_digest,
            "predecessor": expected_completion["predecessor"],
            "after_db": evidence["after_db"],
            "after_files": evidence["after_files"],
            "logs_after": evidence["logs_after"],
        }

    return validate_node(recovery_id)

def validate_group2_recovery(previous, recovery_id=None):
    if previous.get("mode") != carry_forward.GROUP2_MODE:
        return None
    safe = previous.get("carry_forward")
    if not isinstance(safe, dict):
        raise RuntimeError("Group2 deployed state is missing its safe reference")
    manifest_id = safe.get("manifest_id")
    if not isinstance(manifest_id, str) or not carry_forward.MANIFEST_ID.fullmatch(
        manifest_id
    ):
        raise RuntimeError("Invalid consumed Group2 manifest reference")
    active = ROOT / "carry-forward" / "manifests" / f"{manifest_id}.json"
    path = ROOT / "carry-forward" / "consumed" / f"{manifest_id}.json"
    if active.exists() or not path.exists():
        raise RuntimeError("Group2 manifest consumption is incomplete")
    manifest = carry_forward.validate_manifest(carry_forward.read_private(path))
    carry_forward.validate_group2_manifest_extensions(manifest)
    if (
        manifest["to_sha"] != previous.get("sha")
        or manifest["manifest_digest"] != safe.get("manifest_digest")
        or manifest["review_scope_digest"] != safe.get("review_scope_digest")
        or manifest["controller_files_digest"] != controller_files_digest()
    ):
        raise RuntimeError("Consumed Group2 manifest binding changed")
    private_receipt = receipt(previous)
    receipt_evidence = group2_receipt_evidence(private_receipt, manifest)
    safe_result = validate_group2_receipt(
        private_receipt,
        previous,
        manifest,
        receipt_evidence,
    )
    expected_safe = safe_result["carry_forward"]
    if safe != expected_safe:
        raise RuntimeError("Group2 deployed safe reference changed")
    validate_group2_vm_commit(manifest)
    expected_deployment = {
        "db": receipt_evidence["probe"]["after_db"],
        "files": receipt_evidence["probe"]["after_files"],
        "post_preservation": receipt_evidence["probe"]["result"],
        "logs_after": receipt_evidence["post_health"]["logs_after"],
    }
    last_recovery = previous.get("last_recovery")
    tx = transaction()
    if last_recovery is not None:
        if (
            not isinstance(last_recovery, dict)
            or set(last_recovery) != {"recovery_id", "evidence_digest"}
            or re.fullmatch(r"[0-9a-f]{32}", last_recovery["recovery_id"])
            is None
            or re.fullmatch(r"[0-9a-f]{64}", last_recovery["evidence_digest"])
            is None
        ):
            raise RuntimeError("Invalid Group2 recovery safe reference")
        if recovery_id is None:
            node = validate_group2_recovery_evidence(
                manifest, last_recovery["recovery_id"], expected_deployment
            )
            if (
                node["evidence_digest"] != last_recovery["evidence_digest"]
                or tx.get("recovery_id") != last_recovery["recovery_id"]
                or tx.get("recovery_evidence_digest") != node["evidence_digest"]
            ):
                raise RuntimeError("Group2 recovery safe reference changed")
    if recovery_id is not None:
        if not isinstance(recovery_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", recovery_id
        ):
            raise RuntimeError("Invalid Group2 recovery identity")
        node = validate_group2_recovery_evidence(
            manifest, recovery_id, expected_deployment
        )
        expected_predecessor = (
            {"kind": "deployment", "recovery_id": None, "evidence_digest": None}
            if last_recovery is None
            else {"kind": "recovery", **last_recovery}
        )
        if recovery_id != (last_recovery or {}).get("recovery_id") and (
            node["predecessor"] != expected_predecessor
        ):
            raise RuntimeError("Group2 recovery predecessor binding changed")
        if (
            last_recovery is not None
            and recovery_id == last_recovery["recovery_id"]
            and node["evidence_digest"] != last_recovery["evidence_digest"]
        ):
            raise RuntimeError("Group2 recovery safe reference changed")
        recovery_digest = node["evidence_digest"]
        if (
            tx.get("recovery_id") != recovery_id
            or tx.get("recovery_evidence_digest") != recovery_digest
        ):
            raise RuntimeError("Group2 recovery transaction binding changed")
    return manifest


def healthy():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{settings()['web_port']}/api/health", timeout=5
        ) as response:
            body = json.load(response)
            healthy_now = (
                response.status == 200
                and body.get("database") is True
                and body.get("rabbitmq", {}).get("ready") is True
                and preview_containers_running()
            )
            deployed = read("state.json", {})
            if healthy_now and deployed.get("mode") == carry_forward.GROUP2_MODE:
                healthy_now = group2_live_profile(deployed)
            return healthy_now
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


def group2_live_profile(deployed):
    private_receipt = receipt(deployed)
    request = {
        "mode": carry_forward.GROUP2_MODE,
        "operation": "account-check",
        "profile": private_receipt["account_entry"],
        "project": settings()["project"],
        "to_sha": deployed["sha"],
        "candidate_image_ids": deployed["images"],
    }
    vm_private_write(
        "carry-forward/health/request.json",
        carry_forward.canonical_bytes(request) + b"\n",
    )
    result = vm_command(
        "python3",
        vm_path("carry_forward.py"),
        "group2-runtime",
        "--request",
        vm_path("carry-forward/health/request.json"),
        "--output",
        vm_path("carry-forward/health/output.json"),
        check=False,
        timeout=45,
    )
    if result.returncode:
        return False
    output = json.loads(
        vm_command("cat", vm_path("carry-forward/health/output.json")).stdout
    )
    checked = output.get("account_check", {})
    csrf = checked.get("account_csrf", {})
    return (
        checked.get("profile_digest")
        == deployed["carry_forward"].get("account_entry_digest")
        and csrf.get("status") == 200
        and csrf.get("body_status") == "ok"
        and csrf.get("csrf_cookie") is True
        and csrf.get("csrf_cookie_path") is True
        and csrf.get("csrf_cookie_samesite_lax") is True
        and csrf.get("csrf_cookie_httponly") is False
        and csrf.get("redirect") is False
    )


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
    deployed = read("state.json", {})
    if deployed.get("mode") == carry_forward.GROUP2_MODE:
        safe = deployed.get("carry_forward")
        if not isinstance(safe, dict) or safe.get("account_ready") is not True:
            return False
        expected.add(container("account-web"))
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


def _tick():
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
        recovered_manifest = validate_group2_recovery(previous)
        recovery_id = None
        if recovered_manifest is not None:
            recovery_id = uuid.uuid4().hex
            write(
                "attention.json",
                {
                    "phase": "recovering",
                    "sha": previous["sha"],
                    "manifest_id": recovered_manifest["manifest_id"],
                    "recovery_id": recovery_id,
                },
            )
        recovery_target = (
            {**previous, "recovery_id": recovery_id}
            if recovery_id is not None
            else previous
        )
        phase(recovery_target, "recover")
        if recovered_manifest is not None:
            tx = transaction()
            write(
                "attention.json",
                {
                    "phase": "recovering",
                    "sha": previous["sha"],
                    "manifest_id": recovered_manifest["manifest_id"],
                    "recovery_id": recovery_id,
                    "recovery_evidence_digest": tx.get(
                        "recovery_evidence_digest"
                    ),
                },
            )
            validate_group2_recovery(previous, recovery_id)
            previous = {
                **previous,
                "last_recovery": {
                    "recovery_id": recovery_id,
                    "evidence_digest": tx["recovery_evidence_digest"],
                },
            }
            write("state.json", previous)
            (ROOT / "attention.json").unlink()
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
        manifest, manifest_path = selected_manifest(latest_config, previous, target)
        tx = transaction()
        if tx.get("phase") != "ready" or tx.get("sha") != previous["sha"]:
            write("attention.json", tx)
            return status("Unfinished deployment; inspect transaction before retrying")
        status("Switching after CI recheck", candidate=target, deployed=previous)
        # Persist before invoking the VM: process death cannot cause an automatic retry.
        write(
            "attention.json",
            {
                "phase": "switching",
                "candidate": target,
                "deployed": previous,
                "carry_forward": (
                    {
                        "manifest_id": manifest["manifest_id"],
                        "manifest_digest": manifest["manifest_digest"],
                    }
                    if manifest
                    else None
                ),
            },
        )
        if manifest:
            vm_private_write(
                f"carry-forward/manifests/{manifest['manifest_id']}.json",
                manifest_path.read_bytes(),
            )
            if manifest.get("mode") == carry_forward.GROUP2_MODE:
                reviewed = (
                    ROOT
                    / "carry-forward"
                    / "review-scopes"
                    / manifest["review_scope_digest"]
                )
                for source in reviewed.rglob("*"):
                    if source.is_file():
                        vm_private_write(
                            str(
                                Path("carry-forward/review-scopes")
                                / manifest["review_scope_digest"]
                                / source.relative_to(reviewed)
                            ),
                            source.read_bytes(),
                        )
        if not phase(target, "deploy", manifest["manifest_id"] if manifest else ""):
            (ROOT / "attention.json").unlink()
            return status(
                "Waiting for executions to become idle",
                candidate=target,
                deployed=previous,
            )
        deployment_receipt = receipt(target)
        if manifest and manifest.get("mode") == carry_forward.GROUP2_MODE:
            validate_group2_vm_commit(manifest)
            target.update(
                validate_group2_receipt(
                    deployment_receipt,
                    target,
                    manifest,
                    group2_receipt_evidence(deployment_receipt, manifest),
                )
            )
        else:
            target.update(deployment_receipt)
        target["deployed_at"] = datetime.datetime.now().astimezone().isoformat()
        write("state.json", target)
        if manifest:
            consume_manifest(manifest_path)
            latest_config.pop("carry_forward", None)
            write("config.json", latest_config)
        (ROOT / "attention.json").unlink()
        return status("Ready", deployed=target)


def tick():
    """Run one complete controller operation under the cross-command lock."""
    with operation_lock():
        return _tick()


def _read_incident_input(path):
    path = Path(path)
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o177
    ):
        raise ValueError("Incident input must be a private regular file")
    path = path.resolve(strict=True)
    return path, path.read_bytes()


def _incident_tool_files():
    source = Path(__file__).resolve().parent
    values = {}
    for name in carry_forward.GROUP2_CONTROLLER_FILES:
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("Incident source controller is incomplete")
        values[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    repository = source.parents[1]
    head = subprocess.check_output(
        ["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
        env=ENV,
        text=True,
        timeout=30,
    ).strip()
    dirty = subprocess.check_output(
        ["/usr/bin/git", "-C", str(repository), "status", "--porcelain"],
        env=ENV,
        text=True,
        timeout=30,
    )
    if dirty:
        raise ValueError("Incident source controller has uncommitted changes")
    return source, head, values


def _validate_incident_worktree_source(repository, reviewed):
    arguments = [
        "/usr/bin/git", "-C", str(repository), "diff-tree", "--raw", "-r", "-z",
        "--no-abbrev", "--no-renames", "--no-commit-id",
        reviewed["from_sha"], reviewed["to_sha"],
    ]
    raw = subprocess.check_output(arguments, env=ENV, timeout=120)
    if hashlib.sha256(raw).hexdigest() != reviewed["raw_diff_sha256"]:
        raise ValueError("Incident reviewed source raw difference changed")
    for key, suffix in (("from_tree", reviewed["from_sha"]), ("to_tree", reviewed["to_sha"])):
        actual = subprocess.check_output(
            ["/usr/bin/git", "-C", str(repository), "rev-parse", suffix + "^{tree}"],
            env=ENV, text=True, timeout=30,
        ).strip()
        if actual != reviewed[key]:
            raise ValueError("Incident reviewed source tree changed")
    for item in reviewed["entries"]:
        line = subprocess.check_output(
            ["/usr/bin/git", "-C", str(repository), "ls-tree", reviewed["to_sha"], "--", item["path"]],
            env=ENV, text=True, timeout=30,
        ).strip()
        if line != f"{item['new_mode']} blob {item['new_oid']}\t{item['path']}":
            raise ValueError("Incident reviewed source blob changed")


def reconcile_group2_starting(request_path, approval_path):
    """Execute the one-shot, approval-bound Group2 starting reconciliation."""
    request_path, request_bytes = _read_incident_input(request_path)
    approval_path, approval_bytes = _read_incident_input(approval_path)
    if approval_path.name != "USER-APPROVAL.json" or approval_path.parent != request_path.parent:
        raise ValueError("Incident approval must be the fixed sibling approval file")
    _user_record_path, user_record = _read_incident_input(
        approval_path.parent / "USER-APPROVAL.txt"
    )
    try:
        request = json.loads(request_bytes)
        approval = json.loads(approval_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Incident request or approval is invalid") from error
    evidence_directory = request_path.with_suffix(".evidence")
    if not evidence_directory.is_dir() or evidence_directory.is_symlink():
        raise ValueError("Incident evidence directory is missing")
    if evidence_directory.stat().st_mode & 0o077:
        raise ValueError("Incident evidence directory is not private")
    if {item.name for item in evidence_directory.iterdir()} != set(
        carry_forward.GROUP2_RECONCILE_EVIDENCE_FILES
    ):
        raise ValueError("Incident evidence directory is not closed")
    artifacts = {}
    for name in carry_forward.GROUP2_RECONCILE_EVIDENCE_FILES:
        _, artifacts[name] = _read_incident_input(evidence_directory / name)
    try:
        carry_forward.validate_group2_reconcile_user_record(
            request, approval, user_record
        )
    except carry_forward.CarryForwardError as error:
        raise ValueError(
            "Incident approval record does not bind the presented request"
        ) from error
    source, head, tool_files = _incident_tool_files()
    validated = carry_forward.validate_group2_reconcile_request(
        request, approval, artifacts
    )
    if head != request["tool"]["sha"] or tool_files != request["tool"]["controller_files"]:
        raise ValueError("Incident source controller binding changed")
    _validate_incident_worktree_source(
        source.parents[1], validated["artifacts"]["source-review.json"]["source_scope"]
    )
    incident_id = request["incident_id"]
    with operation_lock(blocking=False):
        with config_lock():
            config_bytes = (ROOT / "config.json").read_bytes()
            state_bytes = (ROOT / "state.json").read_bytes()
            attention_bytes = (ROOT / "attention.json").read_bytes()
            config = json.loads(config_bytes)
            state = json.loads(state_bytes)
            attention = json.loads(attention_bytes)
            if (
                config.get("enabled") is not False
                or config.get("repo") != request["repo"]
                or config.get("pr") != request["pr"]
                or state.get("sha") != request["prior"]["sha"]
                or attention.get("candidate", {}).get("sha") != request["failed"]["sha"]
                or attention.get("phase") != "switching"
            ):
                raise ValueError("Incident control-plane authority changed")
            reference = config.get("carry_forward")
            if not isinstance(reference, dict) or any(
                reference.get(key) != request["failed"][key]
                for key in ("manifest_id", "manifest_digest")
            ):
                raise ValueError("Incident failed manifest authority changed")
            failed_active = ROOT / "carry-forward" / "manifests" / f"{request['failed']['manifest_id']}.json"
            failed_consumed = ROOT / "carry-forward" / "consumed" / failed_active.name
            prior_consumed = ROOT / "carry-forward" / "consumed" / f"{request['prior']['manifest_id']}.json"
            if (
                not failed_active.is_file()
                or failed_active.is_symlink()
                or failed_consumed.exists()
                or not prior_consumed.is_file()
                or prior_consumed.is_symlink()
                or hashlib.sha256(prior_consumed.read_bytes()).hexdigest()
                != request["prior"]["consumed_sha256"]
            ):
                raise ValueError("Incident carry-forward lineage changed")
            if controller_files_digest() != request["failed"]["installed_controller_files_digest"]:
                raise ValueError("Installed incident controller identity changed")
            vm_tx = transaction()
            if vm_tx.get("phase") != "starting" or vm_tx.get("sha") != request["failed"]["sha"]:
                raise ValueError("Incident VM transaction authority changed")
            if vm_command("cat", vm_path("current-sha")).stdout.strip() != request["prior"]["sha"]:
                raise ValueError("Incident VM current SHA changed")
            vm_installed = carry_forward._reconcile_vm_installed_files(
                validated["artifacts"]["authority.json"]
            )
            for name, expected in vm_installed.items():
                actual = vm_command("sha256sum", vm_path(name)).stdout.split()[0]
                if actual != expected:
                    raise ValueError("Installed VM controller bytes changed")
            platform = validated["artifacts"]["platform.json"]
            for generation in ("prior", "candidate"):
                for item in platform["images"][generation].values():
                    inspected = json.loads(
                        vm_command("docker", "image", "inspect", item["Id"]).stdout
                    )
                    if len(inspected) != 1 or any(
                        inspected[0].get(key) != value
                        for key, value in item.items()
                        if key in {"Id", "RepoTags", "RepoDigests", "Created", "Architecture", "Os", "RootFS"}
                    ):
                        raise ValueError("Incident image availability changed")
            failed_manifest = validated["artifacts"]["failed-manifest.json"]
            for item in failed_manifest["storage_identity"]:
                if item["type"] == "volume":
                    actual = vm_command(
                        "docker", "volume", "inspect", item["source"], "--format", "{{.Name}}"
                    ).stdout.strip()
                    if actual != item["source"]:
                        raise ValueError("Incident volume identity changed")
                    label = vm_command(
                        "docker", "volume", "inspect", item["source"], "--format",
                        '{{index .Labels "com.docker.compose.project"}}',
                    ).stdout.strip()
                    if label != failed_manifest["account_entry"]["project"]:
                        raise ValueError("Incident volume backing changed")
            project = failed_manifest["account_entry"]["project"]
            schema = vm_command(
                "docker", "exec", f"{project}-postgres-1", "psql", "-U", "dlr", "-d", "dlr", "-Atc",
                "SELECT version_num FROM alembic_version",
            ).stdout.strip()
            pg_version = vm_command(
                "docker", "exec", f"{project}-postgres-1", "postgres", "--version"
            ).stdout.strip()
            server_version = vm_command(
                "docker", "exec", f"{project}-postgres-1", "psql", "-U", "dlr",
                "-d", "dlr", "-Atc", "SHOW server_version",
            ).stdout.strip()
            data_pg_version = vm_command(
                "docker", "exec", f"{project}-postgres-1", "cat",
                "/var/lib/postgresql/data/PG_VERSION",
            ).stdout.strip()
            postgres_format = platform["postgres_format"]
            if (
                schema != request["prior"]["schema"]
                or pg_version != postgres_format["candidate_binary_version"]
                or server_version != postgres_format["current_server_version"]
                or data_pg_version != postgres_format["data_pg_version"]
            ):
                raise ValueError("Incident PostgreSQL format identity changed")
            preflight_authority = {
                "enabled": False,
                "attention_phase": "switching",
                "transaction_phase": "starting",
                "failed_sha": request["failed"]["sha"],
                "current_sha": request["prior"]["sha"],
                "installed_controller_files_digest": request["failed"]["installed_controller_files_digest"],
            }
            host_incident = ROOT / "incidents" / incident_id
            if host_incident.exists():
                raise ValueError("Incident identity was already used")
            if vm_command("test", "!", "-e", vm_path(f"incidents/{incident_id}"), check=False).returncode:
                raise ValueError("Incident identity was already used in the VM")
            host_incident = private_directory(f"incidents/{incident_id}")
            write_private_bytes(host_incident / "request.json", request_bytes)
            write_private_bytes(host_incident / "approval.json", approval_bytes)
            write_private_bytes(host_incident / "USER-APPROVAL.txt", user_record)
            for name, value in artifacts.items():
                write_private_bytes(host_incident / name, value)
            vm_private_write(f"incidents/{incident_id}/request.json", request_bytes)
            vm_private_write(f"incidents/{incident_id}/approval.json", approval_bytes)
            vm_private_write(f"incidents/{incident_id}/USER-APPROVAL.txt", user_record)
            vm_private_write(
                f"incidents/{incident_id}/preflight-authority.json",
                carry_forward.canonical_bytes(preflight_authority),
            )
            for name, value in artifacts.items():
                vm_private_write(f"incidents/{incident_id}/evidence/{name}", value)
            for name in ("deploy.sh", "carry_forward.py"):
                payload = (source / name).read_bytes()
                vm_private_write(f"incidents/{incident_id}/tool/{name}", payload)
                actual = vm_command(
                    "sha256sum", vm_path(f"incidents/{incident_id}/tool/{name}")
                ).stdout.split()[0]
                if actual != tool_files[name]:
                    raise RuntimeError("Incident VM tool transfer changed")
            prepared = carry_forward.canonical_bytes(
                {
                    "schema": "group2-starting-reconcile-phase-v1",
                    "incident_id": incident_id,
                    "phase": "prepared",
                }
            )
            write_private_bytes(host_incident / "phase.json", prepared)
            vm_private_write(f"incidents/{incident_id}/phase.json", prepared)
            with (ROOT / "deploy.log").open("a") as output:
                process = subprocess.Popen(
                    [COLIMA, "ssh", "-p", settings()["profile"], "--", "sudo", "bash",
                     vm_path(f"incidents/{incident_id}/tool/deploy.sh"),
                     "reconcile-group2-starting", settings()["vm_root"], incident_id],
                    env=ENV, stdout=output, stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + 2100
                while True:
                    ready = vm_command(
                        "test", "-f", vm_path(f"incidents/{incident_id}/result.json"), check=False
                    ).returncode == 0
                    if ready:
                        break
                    code = process.poll()
                    if code is not None:
                        raise RuntimeError(f"Incident software reconciliation failed ({code})")
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.wait()
                        raise RuntimeError("Incident software reconciliation timed out")
                    time.sleep(0.25)
            result_bytes = vm_command(
                "cat", vm_path(f"incidents/{incident_id}/result.json")
            ).stdout.encode()
            try:
                result_value = json.loads(result_bytes)
                receipt_value = carry_forward.validate_group2_reconcile_result(
                    request, result_value["evidence"], validated
                )
            except (KeyError, json.JSONDecodeError, carry_forward.CarryForwardError) as error:
                raise RuntimeError("Incident VM result is invalid") from error
            if receipt_value != result_value.get("receipt"):
                raise RuntimeError("Incident VM receipt binding changed")
            vm_receipt_bytes = vm_command(
                "cat", vm_path(f"incidents/{incident_id}/receipt.json")
            ).stdout.encode()
            if json.loads(vm_receipt_bytes) != receipt_value:
                raise RuntimeError("Incident VM receipt file changed")
            write_private_bytes(host_incident / "result.json", result_bytes)
            write_private_bytes(host_incident / "receipt.json", vm_receipt_bytes)
            originals = carry_forward._group2_reconcile_originals(validated)
            chain = {
                "schema": "group2-reconcile-chain-v1",
                "request": request,
                "approval": approval,
                "failed_manifest": originals["manifest"],
                "prior_success": request["prior"],
                "first_startup": {
                    "source_artifacts": {
                        name: {
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "content_b64": base64.b64encode(raw).decode("ascii"),
                        }
                        for name, raw in artifacts.items()
                    },
                    **originals["first_startup"],
                },
                "pre_rollback": result_value["evidence"]["preflight"],
                "stopped": result_value["evidence"]["stopped"],
                "restore_startup": result_value["evidence"]["restore_startup"],
                "restored": {
                    "postgres": result_value["evidence"]["restored_postgres"],
                    "final": result_value["evidence"]["restored"],
                    "account": result_value["evidence"]["account"],
                    "images": result_value["evidence"]["images"],
                    "storage": result_value["evidence"]["storage"],
                    "token_health": result_value["evidence"]["token_health"],
                },
                "receipt": receipt_value,
            }
            preservation = carry_forward.validate_group2_reconcile_preservation(
                chain,
                originals["manifest"]["review_scope"]["preservation_reference"],
            )
            write_private_bytes(
                host_incident / "chain.json", carry_forward.canonical_bytes(chain)
            )
            write_private_bytes(
                host_incident / "preservation-snapshot.json",
                carry_forward.canonical_bytes(preservation),
            )
            vm_private_write(
                f"incidents/{incident_id}/host-validated.json",
                carry_forward.canonical_bytes(
                    {"incident_id": incident_id, "receipt_digest": receipt_value["receipt_digest"]}
                ),
            )
            try:
                code = process.wait(timeout=300)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError("Incident VM transaction commit timed out") from None
            if code:
                raise RuntimeError(f"Incident VM transaction commit failed ({code})")
            committed = transaction()
            if (
                committed.get("phase") != "ready"
                or committed.get("sha") != request["prior"]["sha"]
                or committed.get("operation") != "incident_software_restore"
                or committed.get("reconciled_by")
                != {"incident_id": incident_id, "receipt_digest": receipt_value["receipt_digest"]}
                or committed.get("backup") != state["backup"]
                or committed.get("carry_forward") != state["carry_forward"]
            ):
                raise RuntimeError("Incident VM transaction commit is invalid")
            prior_vm = validated["artifacts"]["prior-success.json"]["vm"]
            for relative, saved in prior_vm.items():
                if saved.get("exists") is True:
                    actual = vm_command("sha256sum", vm_path(relative)).stdout.split()[0]
                    if actual != saved["sha256"]:
                        raise RuntimeError("Incident changed prior success evidence")
                elif (
                    saved != {"exists": False}
                    or vm_command(
                        "test", "!", "-e", vm_path(relative), check=False
                    ).returncode
                ):
                    raise RuntimeError("Incident changed prior success evidence")
            if vm_command("cat", vm_path("current-sha")).stdout.strip() != request["prior"]["sha"]:
                raise RuntimeError("Incident changed current-sha")
            # These files are byte invariants: the incident is not a deployment of a new SHA.
            if (ROOT / "state.json").read_bytes() != state_bytes:
                raise RuntimeError("Incident changed the host deployed state")
            active = ROOT / "carry-forward" / "manifests" / f"{request['failed']['manifest_id']}.json"
            active_bytes = active.read_bytes()
            if hashlib.sha256(active_bytes).hexdigest() != request["failed"]["manifest_sha256"]:
                raise RuntimeError("Incident failed manifest changed")
            abandoned = private_directory("carry-forward/abandoned") / f"{request['failed']['manifest_id']}.json"
            write_private_bytes(abandoned, active_bytes)
            record = {
                "schema": "group2-carry-forward-abandonment-v1",
                "manifest_id": request["failed"]["manifest_id"],
                "manifest_sha256": request["failed"]["manifest_sha256"],
                "incident_id": incident_id,
                "receipt_digest": receipt_value["receipt_digest"],
            }
            write_private_bytes(
                host_incident / "abandonment.json", carry_forward.canonical_bytes(record)
            )
            write_private_bytes(
                abandoned.with_suffix(".abandonment.json"),
                carry_forward.canonical_bytes(record),
            )
            if (ROOT / "config.json").read_bytes() != config_bytes:
                raise RuntimeError("Incident configuration changed before CAS")
            active.unlink()
            config.pop("carry_forward")
            write("config.json", config)
            status("Incident restored prior software; paused", incident=record)
            if (ROOT / "attention.json").read_bytes() != attention_bytes:
                raise RuntimeError("Incident attention changed before final clear")
            (ROOT / "attention.json").unlink()
    return {"incident_id": incident_id, "receipt_digest": receipt_value["receipt_digest"]}


def _embedded_bytes(raw):
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_b64": base64.b64encode(raw).decode("ascii"),
    }


def _validate_partial_review_source(source, head, validated):
    review_value = validated["artifacts"]["source-review.json"]["tool_review"]
    review_raw = base64.b64decode(review_value["content_b64"], validate=True)
    independent = [
        item for item in carry_forward._machine_json_records(review_raw)
        if item.get("schema") == "group2-independent-review-v1"
    ]
    if len(independent) != 1:
        raise ValueError("Partial finalize independent review changed")
    repository = source.parents[1]
    for item in independent[0]["coverage"]:
        line = subprocess.check_output(
            ["/usr/bin/git", "-C", str(repository), "ls-tree", head, "--", item["path"]],
            env=ENV, text=True, timeout=30,
        ).strip()
        match = re.fullmatch(
            r"(100644|100755) blob ([0-9a-f]{40})\t" + re.escape(item["path"]),
            line,
        )
        content = subprocess.check_output(
            ["/usr/bin/git", "-C", str(repository), "show", head + ":" + item["path"]],
            env=ENV, timeout=30,
        )
        if (
            match is None
            or item != {
                "path": item["path"], "mode": match.group(1),
                "blob_oid": match.group(2),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ):
            raise ValueError("Partial finalize independent review changed")


def finalize_group2_partial(request_path, approval_path):
    request_path, request_bytes = _read_incident_input(request_path)
    approval_path, approval_bytes = _read_incident_input(approval_path)
    if approval_path.name != "USER-APPROVAL.json" or approval_path.parent != request_path.parent:
        raise ValueError("Partial finalize approval must be the fixed sibling file")
    _record_path, user_record = _read_incident_input(
        approval_path.parent / "USER-APPROVAL.txt"
    )
    try:
        request = json.loads(request_bytes)
        approval = json.loads(approval_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Partial finalize request or approval is invalid") from error
    evidence_directory = request_path.with_suffix(".evidence")
    if (
        not evidence_directory.is_dir()
        or evidence_directory.is_symlink()
        or evidence_directory.stat().st_mode & 0o077
        or {item.name for item in evidence_directory.iterdir()}
        != carry_forward.GROUP2_PARTIAL_FINALIZE_FILES
    ):
        raise ValueError("Partial finalize evidence directory is invalid")
    artifacts = {
        name: _read_incident_input(evidence_directory / name)[1]
        for name in carry_forward.GROUP2_PARTIAL_FINALIZE_FILES
    }
    validated = carry_forward.validate_group2_partial_finalize_request(
        request, approval, user_record, artifacts
    )
    source, head, tool_files = _incident_tool_files()
    if head != request["tool"]["sha"] or tool_files != request["tool"]["controller_files"]:
        raise ValueError("Partial finalize source controller binding changed")
    _validate_incident_worktree_source(
        source.parents[1], validated["artifacts"]["source-review.json"]["source_scope"]
    )
    _validate_partial_review_source(source, head, validated)
    incident_id = request["incident_id"]
    finalize_id = request["finalize_id"]
    with operation_lock(blocking=False):
        with config_lock():
            config_bytes = (ROOT / "config.json").read_bytes()
            state_bytes = (ROOT / "state.json").read_bytes()
            attention_bytes = (ROOT / "attention.json").read_bytes()
            config = json.loads(config_bytes)
            state = json.loads(state_bytes)
            attention = json.loads(attention_bytes)
            old = validated["context"]["validated_original"]
            old_request = old["request"]
            old_snapshot = old["artifacts"]["authority.json"]["snapshot"]
            if (
                config.get("enabled") is not False
                or config.get("repo") != old_request["repo"]
                or config.get("pr") != old_request["pr"]
                or config.get("carry_forward") != old_snapshot["carry_reference"]
                or state != old_snapshot["state"]
                or attention != old_snapshot["attention"]
            ):
                raise ValueError("Partial finalize host authority changed")
            finalize_root = ROOT / "incidents" / incident_id / "finalize"
            if finalize_root.exists() and any(finalize_root.iterdir()):
                raise ValueError("Partial finalize identity was already used")
            if vm_command(
                "test", "!", "-d", vm_path(f"incidents/{incident_id}/finalize"),
                check=False,
            ).returncode:
                raise ValueError("Partial finalize identity was already used in VM")
            controller_hashes = {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in carry_forward.GROUP2_CONTROLLER_FILES
            }
            expected_controller_hashes = old_snapshot["installed_files"]
            if controller_hashes != expected_controller_hashes:
                raise ValueError("Partial finalize installed controller changed")
            prior = old["artifacts"]["prior-success.json"]
            prior_actual = {"host": {}, "vm": {}}
            for side in ("host", "vm"):
                for relative, expected in prior[side].items():
                    if side == "host":
                        path = ROOT / relative
                        exists = path.is_file() and not path.is_symlink()
                        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest() if exists else None
                    else:
                        exists = vm_command(
                            "test", "-f", vm_path(relative), check=False
                        ).returncode == 0
                        actual_hash = (
                            vm_command("sha256sum", vm_path(relative)).stdout.split()[0]
                            if exists else None
                        )
                    actual = ({"exists": True, "sha256": actual_hash}
                              if exists else {"exists": False})
                    expected_small = ({"exists": True, "sha256": expected.get("sha256")}
                                      if expected.get("exists") is True else {"exists": False})
                    if actual != expected_small:
                        raise ValueError("Partial finalize prior success changed")
                    prior_actual[side][relative] = actual
            host_authority = {
                "host_files": {
                    "config.json": _embedded_bytes(config_bytes),
                    "state.json": _embedded_bytes(state_bytes),
                    "attention.json": _embedded_bytes(attention_bytes),
                },
                "host_installed": controller_hashes,
                "prior_success": prior_actual,
            }
            host_directory = private_directory(
                f"incidents/{incident_id}/finalize/{finalize_id}"
            )
            evidence_host = private_directory(
                f"incidents/{incident_id}/finalize/{finalize_id}/evidence"
            )
            tool_host = private_directory(
                f"incidents/{incident_id}/finalize/{finalize_id}/tool"
            )
            for path, raw in (
                (host_directory / "request.json", request_bytes),
                (host_directory / "approval.json", approval_bytes),
                (host_directory / "USER-APPROVAL.txt", user_record),
            ):
                write_private_bytes(path, raw)
            for name, raw in artifacts.items():
                write_private_bytes(evidence_host / name, raw)
            for name in ("deploy.sh", "carry_forward.py"):
                write_private_bytes(tool_host / name, (source / name).read_bytes())
            phase = carry_forward.canonical_bytes({
                "schema": "group2-partial-finalize-phase-v1",
                "incident_id": incident_id, "finalize_id": finalize_id,
                "phase": "prepared",
            })
            write_private_bytes(host_directory / "phase.json", phase)
            write_private_bytes(
                host_directory / "host-authority.json",
                carry_forward.canonical_bytes(host_authority),
            )
            prefix = f"incidents/{incident_id}/finalize/{finalize_id}"
            for relative, raw in (
                ("request.json", request_bytes), ("approval.json", approval_bytes),
                ("USER-APPROVAL.txt", user_record), ("phase.json", phase),
                ("host-authority.json", carry_forward.canonical_bytes(host_authority)),
            ):
                vm_private_write(f"{prefix}/{relative}", raw)
            for name, raw in artifacts.items():
                vm_private_write(f"{prefix}/evidence/{name}", raw)
            for name in ("deploy.sh", "carry_forward.py"):
                vm_private_write(f"{prefix}/tool/{name}", (source / name).read_bytes())
            with (ROOT / "deploy.log").open("a") as output:
                process = subprocess.Popen(
                    [COLIMA, "ssh", "-p", settings()["profile"], "--", "sudo", "bash",
                     vm_path(f"{prefix}/tool/deploy.sh"), "finalize-group2-partial",
                     settings()["vm_root"], incident_id, finalize_id],
                    env=ENV, stdout=output, stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + 2100
                while vm_command(
                    "test", "-f", vm_path(f"{prefix}/result.json"), check=False
                ).returncode:
                    if process.poll() is not None:
                        raise RuntimeError("Partial finalize VM collection failed")
                    if time.monotonic() >= deadline:
                        process.kill(); process.wait()
                        raise RuntimeError("Partial finalize VM collection timed out")
                    time.sleep(0.25)
            result_bytes = vm_command("cat", vm_path(f"{prefix}/result.json")).stdout.encode()
            result = json.loads(result_bytes)
            receipt = carry_forward.validate_group2_partial_finalize_result(
                validated, result["evidence"]
            )
            if result != {"schema": "group2-partial-finalize-result-v1",
                          "evidence": result["evidence"], "receipt": receipt}:
                raise RuntimeError("Partial finalize VM result is invalid")
            write_private_bytes(host_directory / "result.json", result_bytes)
            receipt_bytes = vm_command("cat", vm_path(f"{prefix}/receipt.json")).stdout.encode()
            if json.loads(receipt_bytes) != receipt:
                raise RuntimeError("Partial finalize receipt changed")
            write_private_bytes(host_directory / "receipt.json", receipt_bytes)
            source_artifacts = {}
            for name in sorted(tuple(artifacts)):
                source_artifacts[name] = _embedded_bytes(artifacts.pop(name))
            gc.collect()
            chain = {
                "schema": "group2-partial-finalize-chain-v1",
                "request": request, "approval": approval,
                "user_record": _embedded_bytes(user_record),
                "source_artifacts": source_artifacts,
                "result": result,
            }
            reference = validated["context"]["originals"]["manifest"][
                "review_scope"
            ]["preservation_reference"]
            snapshot = carry_forward._validate_group2_partial_finalize_preservation(
                chain, reference, validated
            )
            write_private_json_stream(host_directory / "chain.json", chain)
            write_private_bytes(
                host_directory / "preservation-snapshot.json",
                carry_forward.canonical_bytes(snapshot),
            )
            acknowledgement = {
                "finalize_id": finalize_id,
                "request_digest": request["request_digest"],
                "result_digest": carry_forward.digest(result),
                "receipt_digest": receipt["receipt_digest"],
                "chain_digest": carry_forward.streaming_digest(chain),
            }
            vm_private_write(
                f"{prefix}/host-validated.json",
                carry_forward.canonical_bytes(acknowledgement),
            )
            if process.wait(timeout=300):
                raise RuntimeError("Partial finalize VM commit failed")
            committed = transaction()
            if (
                committed.get("phase") != "ready"
                or committed.get("sha") != carry_forward.GROUP2_FROM_SHA
                or committed.get("operation") != "incident_partial_finalize"
                or committed.get("reconciled_by") != {
                    "incident_id": incident_id, "finalize_id": finalize_id,
                    "receipt_digest": receipt["receipt_digest"],
                }
                or committed.get("backup") != state["backup"]
                or committed.get("carry_forward") != state["carry_forward"]
            ):
                raise RuntimeError("Partial finalize VM transaction is invalid")
            if vm_command("cat", vm_path("current-sha")).stdout.strip() != carry_forward.GROUP2_FROM_SHA:
                raise RuntimeError("Partial finalize changed current-sha")
            vm_expected = result["evidence"]["authority"]["vm_installed"]
            for name, expected in vm_expected.items():
                if vm_command("sha256sum", vm_path(name)).stdout.split()[0] != expected:
                    raise RuntimeError("Partial finalize changed VM controller")
            if {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in carry_forward.GROUP2_CONTROLLER_FILES
            } != controller_hashes:
                raise RuntimeError("Partial finalize changed host controller")
            for side, values in prior_actual.items():
                for relative, expected in values.items():
                    if side == "host":
                        path = ROOT / relative
                        exists = path.is_file() and not path.is_symlink()
                        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest() if exists else None
                    else:
                        exists = vm_command(
                            "test", "-f", vm_path(relative), check=False
                        ).returncode == 0
                        actual_hash = (
                            vm_command("sha256sum", vm_path(relative)).stdout.split()[0]
                            if exists else None
                        )
                    actual = ({"exists": True, "sha256": actual_hash}
                              if exists else {"exists": False})
                    if actual != expected:
                        raise RuntimeError("Partial finalize changed prior success evidence")
            if (ROOT / "state.json").read_bytes() != state_bytes:
                raise RuntimeError("Partial finalize changed deployed state")
            active = ROOT / "carry-forward" / "manifests" / (
                old_request["failed"]["manifest_id"] + ".json"
            )
            active_bytes = active.read_bytes()
            if hashlib.sha256(active_bytes).hexdigest() != old_request["failed"]["manifest_sha256"]:
                raise RuntimeError("Partial finalize failed manifest changed")
            abandoned = private_directory("carry-forward/abandoned") / active.name
            write_private_bytes(abandoned, active_bytes)
            abandonment = {
                "schema": "group2-partial-finalize-abandonment-v1",
                "manifest_id": old_request["failed"]["manifest_id"],
                "manifest_sha256": old_request["failed"]["manifest_sha256"],
                "incident_id": incident_id, "finalize_id": finalize_id,
                "receipt_digest": receipt["receipt_digest"],
            }
            write_private_bytes(
                host_directory / "abandonment.json",
                carry_forward.canonical_bytes(abandonment),
            )
            write_private_bytes(
                abandoned.with_suffix(".abandonment.json"),
                carry_forward.canonical_bytes(abandonment),
            )
            if abandoned.read_bytes() != active_bytes:
                raise RuntimeError("Partial finalize abandoned manifest changed")
            if (ROOT / "config.json").read_bytes() != config_bytes:
                raise RuntimeError("Partial finalize config changed before CAS")
            active.unlink()
            config.pop("carry_forward")
            write("config.json", config)
            status("Incident partial finalize completed; paused", incident=abandonment)
            if (ROOT / "attention.json").read_bytes() != attention_bytes:
                raise RuntimeError("Partial finalize attention changed before clear")
            (ROOT / "attention.json").unlink()
            committed_phase = {
                "schema": "group2-partial-finalize-phase-v1",
                "incident_id": incident_id, "finalize_id": finalize_id,
                "phase": "committed",
            }
            write_private_bytes(
                host_directory / "phase.json",
                carry_forward.canonical_bytes(committed_phase),
            )
            vm_private_write(
                f"{prefix}/phase.json",
                carry_forward.canonical_bytes(committed_phase),
            )
    return {"incident_id": incident_id, "finalize_id": finalize_id,
            "receipt_digest": receipt["receipt_digest"]}


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
            "plan-carry-forward",
            "install-group2",
            "reconcile-group2-starting",
            "finalize-group2-partial",
        ],
    )
    parser.add_argument("pr", nargs="?", type=int)
    parser.add_argument("--to-sha")
    parser.add_argument("--ids-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--carry-forward", type=Path)
    parser.add_argument(
        "--mode", choices=(carry_forward.AUDITED_MODE, carry_forward.GROUP2_MODE)
    )
    parser.add_argument("--review-scope", type=Path)
    parser.add_argument("--incident-request", type=Path)
    parser.add_argument("--incident-approval", type=Path)
    parser.add_argument("--finalize-request", type=Path)
    parser.add_argument("--finalize-approval", type=Path)
    args = parser.parse_args()
    if args.command == "finalize-group2-partial":
        if (
            args.pr is not None
            or args.finalize_request is None
            or args.finalize_approval is None
            or any(
                value is not None
                for value in (
                    args.to_sha, args.ids_file, args.output, args.carry_forward,
                    args.mode, args.review_scope, args.incident_request,
                    args.incident_approval,
                )
            )
        ):
            parser.error(
                "finalize-group2-partial requires --finalize-request and --finalize-approval"
            )
        print(json.dumps(finalize_group2_partial(
            args.finalize_request, args.finalize_approval
        ), indent=2))
        return
    if args.finalize_request is not None or args.finalize_approval is not None:
        parser.error("finalize request inputs require finalize-group2-partial")
    if args.command == "reconcile-group2-starting":
        if (
            args.pr is not None
            or args.incident_request is None
            or args.incident_approval is None
            or any(
                value is not None
                for value in (
                    args.to_sha,
                    args.ids_file,
                    args.output,
                    args.carry_forward,
                    args.mode,
                    args.review_scope,
                    args.finalize_request,
                    args.finalize_approval,
                )
            )
        ):
            parser.error(
                "reconcile-group2-starting requires --incident-request and --incident-approval"
            )
        print(
            json.dumps(
                reconcile_group2_starting(
                    args.incident_request, args.incident_approval
                ),
                indent=2,
            )
        )
        return
    if args.command == "install-group2":
        if (
            args.pr is not None
            or not args.review_scope
            or any(
                value is not None
                for value in (
                    args.to_sha,
                    args.ids_file,
                    args.output,
                    args.carry_forward,
                    args.mode,
                    args.finalize_request,
                    args.finalize_approval,
                )
            )
        ):
            parser.error("install-group2 requires --review-scope")
        install_group2(args.review_scope)
        return
    if args.command == "plan-carry-forward":
        if (
            args.pr is not None
            or not args.to_sha
            or not args.ids_file
            or not args.output
        ):
            parser.error(
                "plan-carry-forward requires --to-sha, --ids-file and --output"
            )
        try:
            with operation_lock(blocking=False):
                with config_lock():
                    config = read("config.json")
                    if (
                        not isinstance(config, dict)
                        or config.get("enabled") is not False
                    ):
                        parser.error(
                            "Pause the watcher before creating a carry-forward plan"
                        )
                    result = plan_carry_forward(
                        args.to_sha,
                        args.ids_file,
                        args.output,
                        args.mode,
                        args.review_scope,
                    )
        except BlockingIOError:
            parser.error(
                "Wait for the current controller operation to finish before creating a carry-forward plan"
            )
        print(json.dumps(result, indent=2))
        return
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
                if args.carry_forward:
                    config["carry_forward"] = install_manifest(
                        config, args.pr, args.carry_forward
                    )
                else:
                    config.pop("carry_forward", None)
            elif args.command == "acknowledge":
                tx = transaction()
                previous = read("state.json", {})
                attention = read("attention.json", {})
                if tx.get("phase") != "ready" or tx.get("sha") != previous.get("sha"):
                    parser.error(
                        "Reconcile VM transaction and deployed state first; database rollback is never automatic"
                    )
                recovering_attention = (
                    isinstance(attention, dict)
                    and attention.get("phase") == "recovering"
                )
                recovery_id = attention.get("recovery_id") if recovering_attention else None
                if recovering_attention and (
                    not isinstance(recovery_id, str)
                    or re.fullmatch(r"[0-9a-f]{32}", recovery_id) is None
                ):
                    parser.error("Group2 recovery identity is missing")
                if recovery_id is not None and (
                    attention.get("recovery_evidence_digest")
                    not in {None, tx.get("recovery_evidence_digest")}
                ):
                    parser.error("Group2 recovery evidence binding changed")
                recovered_manifest = validate_group2_recovery(
                    previous, recovery_id
                )
                if recovered_manifest is not None:
                    if recovery_id is not None:
                        previous = {
                            **previous,
                            "last_recovery": {
                                "recovery_id": recovery_id,
                                "evidence_digest": tx[
                                    "recovery_evidence_digest"
                                ],
                            },
                        }
                        write("state.json", previous)
                    reference = config.get("carry_forward")
                    if reference is not None and (
                        not isinstance(reference, dict)
                        or reference.get("manifest_id")
                        != recovered_manifest["manifest_id"]
                    ):
                        parser.error("Group2 consumed manifest reference changed")
                    config.pop("carry_forward", None)
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
