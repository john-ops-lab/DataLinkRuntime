#!/usr/bin/env bash
# Trusted controller, executed only in the dedicated Colima VM.
set -euo pipefail
umask 077
sha=${1:?commit required}
action=${2:?stage, plan, deploy, recover or adopt required}
schema=${3:-}
carry_id=${4:-}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$action" =~ ^(stage|plan|deploy|recover|adopt)$ ]] || exit 2
if [ -n "$carry_id" ]; then [[ "$carry_id" =~ ^[0-9a-f]{32}$ ]] || exit 2; fi
[ "$action" = plan ] || [ -z "$carry_id" ] || [ "$action" = deploy ] || exit 2
root=$(cd "$(dirname "$0")" && pwd)
deployment_settings=$(python3 - "$root/deployment.json" <<'PYSETTINGS'
import json, shlex, sys
with open(sys.argv[1]) as source: settings = json.load(source)
for key in ('project', 'web_port', 'sandbox_unit', 'sandbox_cpu_quota', 'sandbox_memory_max'):
    print(key + '=' + shlex.quote(str(settings[key])))
PYSETTINGS
)
eval "$deployment_settings"
sandbox_description="DataLinkRuntime Sandbox $sandbox_unit CPU=$sandbox_cpu_quota Memory=$sandbox_memory_max"
release=$root/releases/$sha
exec 9>"$root/deploy.lock"
flock -n 9 || exit 1
export COMPOSE_PARALLEL_LIMIT=1
# Existing VM network/proxy configuration is retained; optional installation setting.
if [ -f "$root/build.env" ]; then set -a; source "$root/build.env"; set +a; fi
export NO_PROXY=localhost,127.0.0.1,::1,postgres,rabbitmq,control,worker
mkdir -p "$root/backups"
test -f "$release/.downloaded"
cd "$release"
python3 - "$sha" "$project" <<'PY'
import json, sys
sha, project = sys.argv[1:]
services = {s: {'image': project + '-' + s + ':' + sha, 'pull_policy': 'never'} for s in ('postgres', 'control', 'worker', 'web')}
services['account-web'] = {'image': project + '-web:' + sha, 'pull_policy': 'never'}
for s in ('web', 'account-web'):
    services[s]['environment'] = {k: '' for k in ('HTTP_PROXY','http_proxy','HTTPS_PROXY','https_proxy','ALL_PROXY','all_proxy')}
with open('compose.preview.json', 'w') as out:
    json.dump({'services': services}, out)
PY
compose() { docker compose --project-name "$project" --env-file "$root/preview.env" -f docker-compose.yml -f compose.preview.json "$@"; }
sql() { docker exec "$project-postgres-1" psql -U dlr -d dlr -Atc "$1"; }
record() {
  python3 - "$root/transaction.json" "$1" "$sha" "${backup:-}" "${carry_manifest:-}" <<'PY'
import json, os, sys, time
path, phase, sha, backup, manifest_path = sys.argv[1:]
value = {'phase': phase, 'sha': sha, 'backup': backup, 'at': time.time()}
if manifest_path:
    with open(manifest_path) as source: manifest = json.load(source)
    value['carry_forward'] = {
        'manifest_id': manifest['manifest_id'],
        'manifest_digest': manifest['manifest_digest'],
        'from_sha': manifest['from_sha'],
        'to_sha': manifest['to_sha'],
    }
with open(path + '.tmp', 'w') as out:
    json.dump(value, out)
    out.flush(); os.fsync(out.fileno())
os.replace(path + '.tmp', path)
PY
}
named_volume() {
  local service=$1 target=$2 value
  value=$(docker inspect "$project-$service-1" --format '{{range .Mounts}}{{if eq .Destination "'"$target"'"}}{{.Type}} {{.Name}}{{println}}{{end}}{{end}}')
  [[ "$value" =~ ^volume\ ([a-zA-Z0-9_.-]+)$ ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}
other_named_volume() {
  local service=$1 excluded=$2 value
  value=$(docker inspect "$project-$service-1" --format '{{range .Mounts}}{{if and (eq .Type "volume") (ne .Destination "'"$excluded"'")}}{{.Type}} {{.Name}}{{println}}{{end}}{{end}}')
  [[ "$value" =~ ^volume\ ([a-zA-Z0-9_.-]+)$ ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}
other_named_destination() {
  local service=$1 excluded=$2 value
  value=$(docker inspect "$project-$service-1" --format '{{range .Mounts}}{{if and (eq .Type "volume") (ne .Destination "'"$excluded"'")}}{{.Destination}}{{println}}{{end}}{{end}}')
  [[ "$value" =~ ^/[a-zA-Z0-9_./-]+$ ]] || return 1
  printf '%s\n' "$value"
}
carry_control() {
  local evidence=$1; shift
  local control_network env_file rc
  env_file="$evidence/database.env"
  trap 'rm -f "$env_file"' RETURN EXIT
  python3 - "$project-control-1" "$env_file" <<'PY'
import json, os, pathlib, subprocess, sys
value = json.loads(subprocess.check_output(['docker','inspect',sys.argv[1]], text=True))[0]
allowed = {}
for item in value['Config']['Env']:
    key, separator, raw = item.partition('=')
    if separator and key in {'DATABASE_URL', 'PGOPTIONS'}:
        allowed[key] = raw
assert set(allowed) >= {'DATABASE_URL'}
path = pathlib.Path(sys.argv[2])
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as output:
    for key in ('DATABASE_URL', 'PGOPTIONS'):
        if key in allowed:
            assert '\n' not in allowed[key] and '\r' not in allowed[key]
            output.write(f'{key}={allowed[key]}\n')
PY
  control_network=$(python3 - "$project-control-1" <<'PY'
import json, subprocess, sys
value = json.loads(subprocess.check_output(['docker','inspect',sys.argv[1]], text=True))[0]
networks = list(value['NetworkSettings']['Networks'])
assert len(networks) == 1
print(networks[0])
PY
)
  local -a mounts=(
    --mount "type=bind,source=$root/carry_forward.py,target=/opt/dlr/carry_forward.py,readonly"
    --mount "type=bind,source=$evidence,target=/evidence"
  )
  if [ -n "${carry_manifest:-}" ]; then
    mounts+=(--mount "type=bind,source=$carry_manifest,target=/evidence/manifest.json,readonly")
  fi
  if docker run --rm --read-only --network "$control_network" \
    --cap-drop ALL --cap-add DAC_READ_SEARCH --user 0:0 \
    --security-opt no-new-privileges=true --pids-limit 64 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --env-file "$env_file" --env PYTHONDONTWRITEBYTECODE=1 \
    --entrypoint python "${mounts[@]}" "$control_image" \
    /opt/dlr/carry_forward.py "$@"; then rc=0; else rc=$?; fi
  rm -f "$env_file"
  trap - RETURN EXIT
  return "$rc"
}
carry_files() {
  local evidence=$1; shift
  local -a mounts=(
    --mount "type=bind,source=$root/carry_forward.py,target=/opt/dlr/carry_forward.py,readonly"
    --mount "type=bind,source=$evidence,target=/evidence"
    --mount "type=volume,source=$runtime_volume,target=/var/lib/dlr/runtime,readonly,volume-nocopy"
    --mount "type=volume,source=$journal_volume,target=/var/lib/dlr/journal,readonly,volume-nocopy"
    --mount "type=volume,source=$builtin_volume,target=/var/lib/dlr/builtin-packages,readonly,volume-nocopy"
    --mount "type=volume,source=$artifact_volume,target=$artifact_target,readonly,volume-nocopy"
  )
  if [ -n "${carry_manifest:-}" ]; then
    mounts+=(--mount "type=bind,source=$carry_manifest,target=/evidence/manifest.json,readonly")
  fi
  docker run --rm --read-only --network none --cap-drop ALL \
    --cap-add DAC_READ_SEARCH --user 0:0 \
    --security-opt no-new-privileges=true --pids-limit 64 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --entrypoint python "${mounts[@]}" "$control_image" \
    /opt/dlr/carry_forward.py capture \
      --runtime-root /var/lib/dlr/runtime --journal-root /var/lib/dlr/journal \
      --material-root builtin=/var/lib/dlr/builtin-packages \
      --material-root "artifacts=$artifact_target" \
      --expected-uid "$worker_uid" "$@"
}
images() {
  python3 - "$release/images.json" "${1:-verify}" "$sha" "$project" <<'PY'
import json, subprocess, sys
path, mode, sha, project = sys.argv[1:]
actual = {}
for service in ('postgres','control','worker','web'):
    tag = project + '-' + service + ':' + sha
    actual[tag] = subprocess.check_output(['docker','image','inspect',tag,'--format','{{.Id}}'], text=True).strip()
if mode == 'save':
    with open(path, 'w') as out: json.dump(actual, out)
else:
    with open(path) as source: assert json.load(source) == actual, 'Image identity changed'
PY
}
if [ "$action" = adopt ]; then
  test "$(cat "$root/current-sha")" = "$sha"
  for service in postgres control worker web; do
    image_id=$(docker inspect "$project-$service-1" --format '{{.Image}}')
    docker tag "$image_id" "$project-$service:$sha"
  done
  schema=$(sql 'SELECT version_num FROM alembic_version')
  images save
  printf '%s\n' "$schema" > "$release/schema"
  record ready
  exit 0
fi
if [ "$action" = stage ]; then
  if [ -f "$release/images.json" ]; then images verify; exit 0; fi
  for service in postgres control worker web; do compose build "$service"; done
  images save
  exit 0
fi
images verify
control_image=$(python3 - "$release/images.json" "$project-control:$sha" <<'PY'
import json, sys
with open(sys.argv[1]) as source: images = json.load(source)
value = images.get(sys.argv[2])
assert isinstance(value, str) and value.startswith('sha256:') and len(value) == 71
print(value)
PY
)
if [ "$action" = plan ]; then
  bash "$root/prepare-sandbox-host.sh" --unit "$sandbox_unit" \
    --cpu-quota "$sandbox_cpu_quota" --memory-max "$sandbox_memory_max" --status
  [ -n "$carry_id" ] || exit 2
  carry_work="$root/carry-forward/work/$carry_id"
  test -f "$carry_work/ids.json" && test -f "$carry_work/context.json"
  runtime_volume=$(named_volume worker /var/lib/dlr/runtime)
  journal_volume=$(named_volume worker /var/lib/dlr/journal)
  builtin_volume=$(named_volume control /var/lib/dlr/builtin-packages)
  artifact_volume=$(other_named_volume control /var/lib/dlr/builtin-packages)
  artifact_target=$(other_named_destination control /var/lib/dlr/builtin-packages)
  worker_user=$(docker inspect "$project-worker-1" --format '{{.Config.User}}')
  worker_uid=${worker_user%%:*}; worker_uid=${worker_uid:-0}
  [[ "$worker_uid" =~ ^[0-9]+$ ]]
  for volume in "$runtime_volume" "$journal_volume" "$builtin_volume" "$artifact_volume"; do
    test "$(docker volume inspect "$volume" --format '{{.Name}}')" = "$volume"
    test "$(docker volume inspect "$volume" --format '{{index .Labels "com.docker.compose.project"}}')" = "$project"
  done
  carry_control "$carry_work" check-db \
    --ids /evidence/ids.json --output /evidence/db.json
  carry_files "$carry_work" --db /evidence/db.json --output /evidence/files.json
  python3 "$root/carry_forward.py" check-kernel --unit "$sandbox_unit" \
    --expected-description "$sandbox_description" \
    --output "$carry_work/kernel.json"
  python3 "$root/carry_forward.py" plan \
    --ids "$carry_work/ids.json" --context "$carry_work/context.json" \
    --db "$carry_work/db.json" --files "$carry_work/files.json" \
    --kernel "$carry_work/kernel.json" --output "$carry_work/manifest.json"
  exit 0
fi
if [ -n "$carry_id" ]; then
  bash "$root/prepare-sandbox-host.sh" --unit "$sandbox_unit" \
    --cpu-quota "$sandbox_cpu_quota" --memory-max "$sandbox_memory_max" --status
else
  bash "$root/prepare-sandbox-host.sh" --unit "$sandbox_unit" \
    --cpu-quota "$sandbox_cpu_quota" --memory-max "$sandbox_memory_max"
fi
if [ "$action" = recover ]; then
  python3 - "$root/transaction.json" "$sha" <<'PY'
import json, sys
with open(sys.argv[1]) as source: t = json.load(source)
assert t['phase'] == 'ready' and t['sha'] == sys.argv[2], 'Unfinished deployment'
PY
  compose up -d --no-build --wait --wait-timeout 180 postgres rabbitmq
  test "$(sql 'SELECT version_num FROM alembic_version')" = "$(cat "$release/schema")"
  compose up -d --no-build --wait --wait-timeout 180 control worker web
else
  carry_manifest=
  if [ -n "$carry_id" ]; then
    carry_manifest="$root/carry-forward/manifests/$carry_id.json"
    test -f "$carry_manifest"
    runtime_volume=$(named_volume worker /var/lib/dlr/runtime)
    journal_volume=$(named_volume worker /var/lib/dlr/journal)
    builtin_volume=$(named_volume control /var/lib/dlr/builtin-packages)
    artifact_volume=$(other_named_volume control /var/lib/dlr/builtin-packages)
    artifact_target=$(other_named_destination control /var/lib/dlr/builtin-packages)
    worker_user=$(docker inspect "$project-worker-1" --format '{{.Config.User}}')
    worker_uid=${worker_user%%:*}; worker_uid=${worker_uid:-0}
    [[ "$worker_uid" =~ ^[0-9]+$ ]]
    for volume in "$runtime_volume" "$journal_volume" "$builtin_volume" "$artifact_volume"; do
      test "$(docker volume inspect "$volume" --format '{{.Name}}')" = "$volume"
      test "$(docker volume inspect "$volume" --format '{{index .Labels "com.docker.compose.project"}}')" = "$project"
    done
    python3 - "$root/carry_forward.py" "$carry_manifest" "$root/current-sha" \
      "$release/images.json" "$root/releases/$(cat "$root/current-sha")/images.json" \
      "$schema" "$project" "$root" <<'PY'
import importlib.util, json, pathlib, subprocess, sys
script, manifest_path, current_path, candidate_images_path, old_images_path, schema, project, controller_root = sys.argv[1:]
spec = importlib.util.spec_from_file_location('carry_forward', script)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
manifest = module.validate_manifest(json.loads(pathlib.Path(manifest_path).read_text()))
assert pathlib.Path(current_path).read_text().strip() == manifest['from_sha']
assert manifest['to_sha'] == pathlib.Path(candidate_images_path).parent.name
assert manifest['to_schema'] == schema
assert manifest['candidate_image_ids'] == json.loads(pathlib.Path(candidate_images_path).read_text())
assert manifest['old_image_ids'] == json.loads(pathlib.Path(old_images_path).read_text())
actual = []
for service in ('postgres', 'rabbitmq', 'control', 'worker'):
    mounts = json.loads(subprocess.check_output(
        ['docker','inspect',f'{project}-{service}-1','--format','{{json .Mounts}}'], text=True
    ))
    actual.extend(
        {'service':service,'type':'volume','name':item.get('Name'),'destination':item.get('Destination')}
        for item in mounts if item.get('Type') == 'volume'
    )
actual.sort(key=lambda item:(item['service'], item['destination']))
assert manifest['storage_identity'] == actual
release = pathlib.Path(candidate_images_path).parent
config = json.loads(subprocess.check_output([
    'docker','compose','--project-name',project,'--env-file',str(pathlib.Path(controller_root)/'preview.env'),
    '-f',str(release/'docker-compose.yml'),'-f',str(release/'compose.preview.json'),
    'config','--format','json',
], text=True))
candidate = []
declared = config.get('volumes', {})
for service in ('postgres', 'rabbitmq', 'control', 'worker'):
    for item in config['services'][service].get('volumes', []):
        if item.get('type') != 'volume':
            continue
        source = item.get('source')
        assert isinstance(source, str) and source in declared
        volume = declared[source]
        name = volume.get('name') or f'{project}_{source}'
        candidate.append({
            'service':service,'type':'volume','name':name,'destination':item.get('target')
        })
candidate.sort(key=lambda item:(item['service'], item['destination']))
assert candidate == manifest['storage_identity']
PY
    carry_check="$root/carry-forward/check/$carry_id"
    install -d -m 700 "$carry_check/preflight"
    carry_control "$carry_check/preflight" check-db \
      --baseline /evidence/manifest.json --output /evidence/db.json
    carry_files "$carry_check/preflight" \
      --baseline /evidence/manifest.json --output /evidence/files.json
  fi
  # Delay while queued/running/retrying work or unfinished cleanup exists.
  busy_query="SELECT (SELECT count(*) FROM executions WHERE status IN ('queued','running','retry_wait') OR workspace_cleanup_status='pending') + (SELECT count(*) FROM execution_attempts WHERE status IN ('claimed','running'))"
  if [ -z "$carry_manifest" ]; then test "$(sql "$busy_query")" = 0 || exit 75; fi
  old=$(cat "$root/current-sha")
  old_compose() { docker compose --project-name "$project" --env-file "$root/preview.env" -f "$root/releases/$old/docker-compose.yml" -f "$root/releases/$old/compose.preview.json" "$@"; }
  restore_old_apps() {
    old_compose up -d --no-build --wait --wait-timeout 180 control worker web
    python3 - "$root/transaction.json" "$old" <<'PY'
import json, os, sys
path, sha = sys.argv[1:]
with open(path + '.tmp', 'w') as out:
    json.dump({'phase':'ready','sha':sha},out); out.flush(); os.fsync(out.fileno())
os.replace(path + '.tmp', path)
PY
  }
  record quiescing
  # This stops API, schedules, retries and dispatch creation before the second check.
  old_compose stop control
  if [ -n "$carry_manifest" ]; then
    install -d -m 700 "$carry_check/control-stopped"
    if ! carry_control "$carry_check/control-stopped" check-db \
      --baseline /evidence/manifest.json --output /evidence/db.json \
      > "$carry_check/control-stopped/stdout.jsonl" \
      2> "$carry_check/control-stopped/stderr.jsonl"; then
      exit 1
    fi
  elif [ "$(sql "$busy_query")" != 0 ]; then
    restore_old_apps
    exit 75
  fi
  old_compose stop worker web account-web
  if [ -n "$carry_manifest" ]; then
    for service in control worker web account-web; do
      service_id=$(old_compose ps -a -q "$service")
      if [ -n "$service_id" ]; then
        test "$(docker inspect "$service_id" --format '{{.State.Running}}')" = false
      fi
    done
    unknown_running=$(docker ps \
      --filter "label=com.docker.compose.project=$project" \
      --format '{{.Label "com.docker.compose.service"}}' \
      | grep -Ev '^(postgres|rabbitmq)$' || true)
    test -z "$unknown_running"
    install -d -m 700 "$carry_check/stopped"
    if ! carry_control "$carry_check/stopped" check-db \
      --baseline /evidence/manifest.json --output /evidence/db.json \
      > "$carry_check/stopped/db-stdout.jsonl" \
      2> "$carry_check/stopped/db-stderr.jsonl"; then
      exit 1
    fi
    carry_files "$carry_check/stopped" \
      --baseline /evidence/manifest.json --output /evidence/files.json
    python3 "$root/carry_forward.py" check-kernel \
      --unit "$sandbox_unit" --expected-description "$sandbox_description" \
      --require-idle --baseline "$carry_manifest" \
      --output "$carry_check/stopped/kernel.json"
  fi
  backup="$root/backups/$(date -u +%Y%m%dT%H%M%SZ)-$old-to-$sha"
  mkdir -p "$backup"
  record backing_up
  docker exec "$project-postgres-1" pg_dump -U dlr -d dlr -Fc > "$backup/database.dump"
  docker exec -i "$project-postgres-1" pg_restore --list < "$backup/database.dump" > "$backup/database.list"
  test -s "$backup/database.list"
  compose run --rm --no-deps -T -v "$root:/preview:ro" control python /preview/assets.py > "$backup/assets.json"
  if [ -n "$carry_manifest" ]; then
    install -d -m 700 "$carry_check/after-backup"
    carry_control "$carry_check/after-backup" check-db \
      --baseline /evidence/manifest.json --output /evidence/db.json
    carry_files "$carry_check/after-backup" \
      --baseline /evidence/manifest.json --output /evidence/files.json
    python3 "$root/carry_forward.py" check-kernel \
      --unit "$sandbox_unit" --expected-description "$sandbox_description" \
      --require-idle --baseline "$carry_manifest" \
      --output "$carry_check/after-backup/kernel.json"
  fi
  compose up -d --no-build --wait --wait-timeout 180 postgres rabbitmq
  record migrating
  compose run --rm --no-deps -T control alembic upgrade head
  test "$(sql 'SELECT version_num FROM alembic_version')" = "$schema"
  compose run --rm --no-deps -T -v "$root:/preview:ro" control python /preview/assets.py "/preview/backups/$(basename "$backup")/assets.json" > "$backup/assets-after.json"
  if [ -n "$carry_manifest" ]; then
    install -d -m 700 "$carry_check/after-migration"
    carry_control "$carry_check/after-migration" check-db \
      --baseline /evidence/manifest.json --output /evidence/db.json
    carry_files "$carry_check/after-migration" \
      --baseline /evidence/manifest.json --output /evidence/files.json
    python3 "$root/carry_forward.py" check-kernel \
      --unit "$sandbox_unit" --expected-description "$sandbox_description" \
      --require-idle --baseline "$carry_manifest" \
      --output "$carry_check/after-migration/kernel.json"
  fi
  compose up -d --no-build --wait --wait-timeout 180 control worker web
fi
curl --fail --silent "http://127.0.0.1:$web_port/api/health"
worker_id=$(compose ps -q worker)
test "$(docker inspect "$worker_id" --format '{{.HostConfig.CgroupnsMode}}:{{.HostConfig.Privileged}}')" = private:false
compose exec -T worker python -c 'import os; assert os.path.exists("/tmp/dlr-worker.ready"); assert open("/proc/1/cgroup").read().strip() in {"0::/", "0::/agent"}; assert os.path.isdir("/run/dlr-cgroup/agent"); assert open("/run/dlr-cgroup/memory.max").read().strip() != "max"; assert open("/run/dlr-cgroup/cpu.max").read().split()[0] != "max"'
# Verify the running containers use the exact recorded images.
for service in postgres control worker web; do
  test "$(docker inspect "$project-$service-1" --format '{{.Image}}')" = "$(docker image inspect "$project-$service:$sha" --format '{{.Id}}')"
done
if [ "$action" = deploy ]; then
  record verifying
  compose exec -T control python - < "$root/verify.py" > "$release/probe.json"
  printf '%s\n' "$schema" > "$release/schema"
  python3 - "$release" "$backup" "${carry_manifest:-}" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
carry = None
if sys.argv[3]:
    with open(sys.argv[3]) as source: manifest = json.load(source)
    carry = {
        'manifest_id': manifest['manifest_id'],
        'manifest_digest': manifest['manifest_digest'],
        'selection_count': len(manifest['responsibilities']['executions']),
    }
with (root / 'receipt.json').open('w') as out:
    json.dump({'schema': (root/'schema').read_text().strip(), 'images': json.loads((root/'images.json').read_text()),
               'probe': json.loads((root/'probe.json').read_text()), 'backup':sys.argv[2],
               'carry_forward': carry}, out)
PY
  printf '%s\n' "$sha" > "$root/current-sha.tmp"
  mv "$root/current-sha.tmp" "$root/current-sha"
  record ready
fi
compose ps
