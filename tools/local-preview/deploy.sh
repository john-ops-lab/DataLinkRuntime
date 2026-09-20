#!/usr/bin/env bash
# Trusted controller, executed only in the dedicated Colima VM.
set -euo pipefail
umask 077
sha=${1:?commit required}
action=${2:?stage, plan, deploy, recover or adopt required}
schema=${3:-}
carry_id=${4:-}
recovery_id=${5:-}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$action" =~ ^(stage|plan|deploy|recover|adopt)$ ]] || exit 2
if [ -n "$carry_id" ]; then [[ "$carry_id" =~ ^[0-9a-f]{32}$ ]] || exit 2; fi
[ "$action" = plan ] || [ -z "$carry_id" ] || [ "$action" = deploy ] || exit 2
if [ -n "$recovery_id" ]; then
  [ "$action" = recover ] && [[ "$recovery_id" =~ ^[0-9a-f]{32}$ ]] || exit 2
fi
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
  python3 - "$root/transaction.json" "$1" "$sha" "${backup:-}" \
    "${carry_manifest:-}" "${recovery_id:-}" "${recovery_completion_digest:-}" <<'PY'
import json, os, sys, time
path, phase, sha, backup, manifest_path, recovery_id, recovery_digest = sys.argv[1:]
value = {'phase': phase, 'sha': sha, 'backup': backup, 'at': time.time()}
if manifest_path:
    with open(manifest_path) as source: manifest = json.load(source)
    value['carry_forward'] = {
        'manifest_id': manifest['manifest_id'],
        'manifest_digest': manifest['manifest_digest'],
        'from_sha': manifest['from_sha'],
        'to_sha': manifest['to_sha'],
    }
if recovery_id:
    value['recovery_id'] = recovery_id
if recovery_digest:
    value['recovery_evidence_digest'] = recovery_digest
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
carry_state() {
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
    --mount "type=volume,source=$runtime_volume,target=/var/lib/dlr/runtime,readonly,volume-nocopy"
    --mount "type=volume,source=$journal_volume,target=/var/lib/dlr/journal,readonly,volume-nocopy"
    --mount "type=volume,source=$builtin_volume,target=/var/lib/dlr/builtin-packages,readonly,volume-nocopy"
    --mount "type=volume,source=$artifact_volume,target=$artifact_target,readonly,volume-nocopy"
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
    /opt/dlr/carry_forward.py capture-state \
      --runtime-root /var/lib/dlr/runtime --journal-root /var/lib/dlr/journal \
      --material-root builtin=/var/lib/dlr/builtin-packages \
      --material-root "artifacts=$artifact_target" \
      --expected-uid "$worker_uid" "$@"; then rc=0; else rc=$?; fi
  rm -f "$env_file"
  trap - RETURN EXIT
  return "$rc"
}
group2_runtime_host() {
  local request=$1 output=$2
  python3 "$root/carry_forward.py" group2-runtime \
    --request "$request" --output "$output" >/dev/null
}
group2_log_checkpoint() {
  local baseline=$1 output=$2 request="${2%.json}-request.json"
  python3 - "$request" "$baseline" <<'PY'
import json, os, sys
baseline=json.load(open(sys.argv[2])); baseline=baseline.get('log_evidence',baseline)
value={'mode':'audited-group2-same-schema-v1','operation':'log-append','baseline':baseline}
with open(sys.argv[1]+'.tmp','w') as out: json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$request" "$output"
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
  carry_mode=$(python3 - "$carry_work/context.json" <<'PY'
import json, sys
with open(sys.argv[1]) as source: value = json.load(source)
print(value.get('mode', ''))
PY
)
  mode_args=()
  if [ -n "$carry_mode" ]; then mode_args=(--mode "$carry_mode"); fi
  if [ "$carry_mode" = audited-group2-same-schema-v1 ]; then
    python3 - "$carry_work/account-request.json" "$project" \
      "$(cat "$root/current-sha")" "$sha" "$root" <<'PY'
import json, os, sys
path, project, old_sha, new_sha, root = sys.argv[1:]
value = {
    'mode':'audited-group2-same-schema-v1', 'operation':'account-capture',
    'project':project, 'from_sha':old_sha, 'to_sha':new_sha,
    'old_release':f'{root}/releases/{old_sha}',
    'candidate_release':f'{root}/releases/{new_sha}',
    'env_file':f'{root}/preview.env',
    'old_image_ids':json.load(open(f'{root}/releases/{old_sha}/images.json')),
    'candidate_image_ids':json.load(open(f'{root}/releases/{new_sha}/images.json')),
}
with open(path + '.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(path + '.tmp',path)
PY
    group2_runtime_host "$carry_work/account-request.json" "$carry_work/account.json"
    python3 - "$carry_work/log-request.json" "$carry_work/account.json" <<'PY'
import json, os, sys
account=json.load(open(sys.argv[2]))
value={'mode':'audited-group2-same-schema-v1','operation':'log-capture',
       'profile':account['account_entry']}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
    group2_runtime_host "$carry_work/log-request.json" "$carry_work/log.json"
    python3 - "$carry_work/context.json" "$carry_work/account.json" \
      "$carry_work/log.json" <<'PY'
import json, os, sys
path=sys.argv[1]; value=json.load(open(path))
value['account_entry']=json.load(open(sys.argv[2]))['account_entry']
value['log_evidence']=json.load(open(sys.argv[3]))['log_evidence']
with open(path+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(path+'.tmp',path)
PY
  fi
  carry_state "$carry_work" --ids /evidence/ids.json "${mode_args[@]}" \
    --db-output /evidence/db.json --files-output /evidence/files.json
  python3 "$root/carry_forward.py" check-kernel --unit "$sandbox_unit" \
    --expected-description "$sandbox_description" \
    --worker-container "$project-worker-1" \
    --runtime-volume "$runtime_volume" --journal-volume "$journal_volume" \
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
  test "$(cat "$root/current-sha")" = "$sha"
  is_group2=$(python3 - "$release/receipt.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1]))
print('true' if value.get('mode') == 'audited-group2-same-schema-v1' else 'false')
PY
  )
  if [ "$is_group2" = true ]; then
    [ -n "$recovery_id" ] || exit 2
    read -r previous_recovery_id previous_recovery_digest < <(
      python3 - "$root/transaction.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1])); identity=value.get('recovery_id'); digest=value.get('recovery_evidence_digest')
if identity is None and digest is None: print('- -')
elif isinstance(identity,str) and isinstance(digest,str): print(identity,digest)
else: raise SystemExit('Invalid previous recovery binding')
PY
    )
    record recovering
  fi
  compose up -d --no-build --wait --wait-timeout 180 postgres rabbitmq
  test "$(sql 'SELECT version_num FROM alembic_version')" = "$(cat "$release/schema")"
  if [ "$is_group2" = true ]; then
    recovery=$(python3 - "$root/carry-forward" "$sha" "$recovery_id" <<'PY'
import pathlib, sys
path=pathlib.Path(sys.argv[1])/f'recovery-{sys.argv[2]}-{sys.argv[3]}'
path.mkdir(mode=0o700,parents=True,exist_ok=False)
print(path)
PY
    )
    carry_manifest=$(python3 - "$root" "$release/receipt.json" "$sha" \
      "$root/carry_forward.py" <<'PY'
import importlib.util, json, pathlib, sys
root=pathlib.Path(sys.argv[1]); receipt=json.load(open(sys.argv[2])); sha=sys.argv[3]
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[4])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
reference=receipt['carry_forward']; manifest_path=root/'carry-forward'/'manifests'/(reference['manifest_id']+'.json')
manifest=m.validate_manifest(m.read_private(manifest_path)); m.validate_group2_manifest_extensions(manifest)
assert manifest['to_sha']==sha
assert manifest['manifest_digest']==reference['manifest_digest']
assert manifest['review_scope_digest']==receipt['review_scope_digest']
print(manifest_path)
PY
    )
    runtime_volume=$(named_volume worker /var/lib/dlr/runtime)
    journal_volume=$(named_volume worker /var/lib/dlr/journal)
    builtin_volume=$(named_volume control /var/lib/dlr/builtin-packages)
    artifact_volume=$(other_named_volume control /var/lib/dlr/builtin-packages)
    artifact_target=$(other_named_destination control /var/lib/dlr/builtin-packages)
    worker_user=$(docker inspect "$project-worker-1" --format '{{.Config.User}}')
    worker_uid=${worker_user%%:*}; worker_uid=${worker_uid:-0}
    [[ "$worker_uid" =~ ^[0-9]+$ ]]
    install -m 600 "$release/recovery-baseline/ids.json" "$recovery/ids.json"
    carry_state "$recovery" --ids /evidence/ids.json \
      --mode audited-group2-same-schema-v1 \
      --db-output /evidence/before-db.json --files-output /evidence/before-files.json
    python3 - "$root/carry_forward.py" "$release/receipt.json" \
      "$release/recovery-baseline" "$recovery/before-db.json" \
      "$recovery/before-files.json" "$carry_manifest" "$root/carry-forward" \
      "$previous_recovery_id" "$previous_recovery_digest" "$recovery/baseline.json" <<'PY'
import importlib.util, json, pathlib, re, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
receipt=json.load(open(sys.argv[2])); baseline=pathlib.Path(sys.argv[3])
db=json.load(open(baseline/'db.json')); files=json.load(open(baseline/'files.json'))
logs=json.load(open(baseline/'logs-after.json'))['log_evidence']
actual_db=json.load(open(sys.argv[4])); actual_files=json.load(open(sys.argv[5]))
value={'result':json.load(open(baseline/'post-preservation.json')),'db':db,'files':files}
assert m.digest(value)==receipt['post_preservation_digest']==receipt['stages']['post_preservation']
manifest=m.validate_manifest(m.read_private(pathlib.Path(sys.argv[6]))); root=pathlib.Path(sys.argv[7])
group2=root/'check'/manifest['manifest_id']/'group2'
post_health={'account_check':json.load(open(group2/'account-after.json'))['account_check'],
             'entry_probe':json.load(open(group2/'entry-after.json'))['entry_probe'],
             'logs_after':json.load(open(group2/'log-after-health.json'))['log_evidence']}
assert m.digest(post_health)==receipt['stages']['post_health']
assert logs==post_health['logs_after']
previous_id,previous_digest=sys.argv[8:10]
deployment={'db':db,'files':files,'post_preservation':value['result'],'logs_after':logs}
visiting=set()
def validate_prior(identity, expected_digest):
    assert isinstance(identity,str) and re.fullmatch(r'[0-9a-f]{32}',identity)
    assert isinstance(expected_digest,str) and re.fullmatch(r'[0-9a-f]{64}',expected_digest)
    assert identity not in visiting
    visiting.add(identity)
    try:
        prior=root/f'recovery-{manifest["to_sha"]}-{identity}'
        def read(name): return json.load(open(prior/name))
        prior_baseline=read('baseline.json'); completion=read('completion.json')
        assert prior_baseline['deployment']==deployment
        reference=prior_baseline['predecessor']
        if reference['kind']=='deployment':
            expected={'kind':'deployment','recovery_id':None,'evidence_digest':None,
                      'db':db,'files':files,'logs_after':logs}
        elif reference.get('kind')=='recovery':
            parent=validate_prior(reference['recovery_id'],reference['evidence_digest'])
            expected={'kind':'recovery','recovery_id':parent['recovery_id'],
                      'evidence_digest':parent['evidence_digest'],'db':parent['after_db'],
                      'files':parent['after_files'],'logs_after':parent['logs_after']}
        else:
            raise AssertionError('Invalid recovery predecessor kind')
        assert reference==expected
        evidence={'request':read('startup-request.json'),'proof':read('startup.json')['startup_proof'],
          'before_db':read('before-db.json'),'after_db':read('after-db.json'),
          'before_files':read('before-files.json'),'after_files':read('after-files.json'),
          'account_check':read('account.json')['account_check'],'entry_probe':read('entry.json')['entry_probe'],
          'logs_before':read('log-before.json')['log_evidence'],'logs_after':read('log-after.json')['log_evidence'],
          'preservation':read('preservation.json')}
        result=m.validate_group2_recovery_evidence(manifest,prior_baseline,evidence,deployment)
        evidence_digest=m.digest({'baseline':prior_baseline,'evidence':evidence})
        expected_completion={'schema':'group2-recovery-completion-v1','recovery_id':identity,
          'sha':manifest['to_sha'],'manifest_id':manifest['manifest_id'],
          'manifest_digest':manifest['manifest_digest'],'evidence_digest':evidence_digest,
          'predecessor':{key:expected[key] for key in ('kind','recovery_id','evidence_digest')},
          'result':result}
        assert completion==expected_completion and evidence_digest==expected_digest
        return {'recovery_id':identity,'evidence_digest':evidence_digest,
                'after_db':evidence['after_db'],'after_files':evidence['after_files'],
                'logs_after':evidence['logs_after']}
    finally:
        visiting.remove(identity)
if previous_id=='-':
    assert previous_digest=='-'
    predecessor={'kind':'deployment','recovery_id':None,'evidence_digest':None,
                 'db':db,'files':files,'logs_after':logs}
else:
    prior=validate_prior(previous_id,previous_digest)
    predecessor={'kind':'recovery','recovery_id':prior['recovery_id'],
                 'evidence_digest':prior['evidence_digest'],'db':prior['after_db'],
                 'files':prior['after_files'],'logs_after':prior['logs_after']}
current={'deployment':deployment,'predecessor':predecessor,
         'fresh':{'db':actual_db,'files':actual_files}}
m._validate_group2_recovery_lineage(deployment,predecessor,current['fresh'])
m.write_private(pathlib.Path(sys.argv[10]),current)
PY
    python3 - "$recovery/before-worker.json" "$root/carry_forward.py" "$project" <<'PY'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[2]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.write_private(pathlib.Path(sys.argv[1]),m._inspect_container(sys.argv[3],'worker'))
PY
    python3 - "$recovery/log-before-request.json" "$recovery/baseline.json" <<'PY'
import json, os, sys
baseline=json.load(open(sys.argv[2])); value={'mode':'audited-group2-same-schema-v1','operation':'log-append','baseline':baseline['predecessor']['logs_after']}
with open(sys.argv[1]+'.tmp','w') as out: json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
    group2_runtime_host "$recovery/log-before-request.json" "$recovery/log-before.json"
    python3 - "$recovery/start-window.json" <<'PY'
import json, os, sys, time
with open(sys.argv[1]+'.tmp','w') as out: json.dump({'window_start_ns':time.time_ns()},out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
    compose up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
    compose up -d --no-build --wait --wait-timeout 180 control web account-web
    python3 - "$recovery/start-window.json" <<'PY'
import json, os, sys, time
path=sys.argv[1]; value=json.load(open(path)); value['window_end_ns']=time.time_ns()
with open(path+'.tmp','w') as out: json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(path+'.tmp',path)
PY
    python3 - "$recovery/account-request.json" "$release/receipt.json" "$project" "$sha" "$release/images.json" <<'PY'
import json, os, sys
receipt=json.load(open(sys.argv[2]))
value={'mode':'audited-group2-same-schema-v1','operation':'account-check',
       'profile':receipt['account_entry'],'project':sys.argv[3],
       'to_sha':sys.argv[4],'candidate_image_ids':json.load(open(sys.argv[5]))}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
    group2_runtime_host "$recovery/account-request.json" "$recovery/account.json"
    python3 - "$recovery/log-after-request.json" "$recovery/log-before.json" <<'PY'
import json, os, sys
value={'mode':'audited-group2-same-schema-v1','operation':'log-append','baseline':json.load(open(sys.argv[2]))['log_evidence']}
with open(sys.argv[1]+'.tmp','w') as out: json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
    group2_runtime_host "$recovery/log-after-request.json" "$recovery/log-after.json"
    python3 - "$recovery/startup-request.json" "$release/receipt.json" \
      "$recovery/log-before.json" "$recovery/log-after.json" \
      "$recovery/before-worker.json" "$recovery/account.json" \
      "$recovery/start-window.json" <<'PY'
import json, os, sys
receipt=json.load(open(sys.argv[2])); checked=json.load(open(sys.argv[6])); window=json.load(open(sys.argv[7]))
value={'mode':'audited-group2-same-schema-v1','operation':'startup-proof','profile':receipt['account_entry'],
       'logs_before':json.load(open(sys.argv[3]))['log_evidence'],'logs_after':json.load(open(sys.argv[4]))['log_evidence'],
       'container_before':json.load(open(sys.argv[5])),'container_after':checked['account_check']['containers']['worker'],**window}
with open(sys.argv[1]+'.tmp','w') as out: json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
    group2_runtime_host "$recovery/startup-request.json" "$recovery/startup.json"
    carry_state "$recovery" --ids /evidence/ids.json \
      --mode audited-group2-same-schema-v1 \
      --db-output /evidence/after-db.json --files-output /evidence/after-files.json
    python3 - "$root/carry_forward.py" "$recovery/before-db.json" \
      "$recovery/after-db.json" "$recovery/before-files.json" \
      "$recovery/after-files.json" "$recovery/startup.json" \
      "$recovery/preservation.json" <<'PY'
import importlib.util, json, pathlib, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
before=json.load(open(sys.argv[2])); after=json.load(open(sys.argv[3])); files_before=json.load(open(sys.argv[4])); files_after=json.load(open(sys.argv[5]))
for key in ('projection','responsibilities','protected_rows','asset_projection','schema_shape','schema_inventory'):
    if before[key]!=after[key]: raise m.CarryForwardError('group2_recovery_db_changed')
result=m.compare_group2_startup_files(files_before,files_after,json.load(open(sys.argv[6]))['startup_proof'])
m.write_private(pathlib.Path(sys.argv[7]),result)
PY
    python3 - "$recovery/entry-request.json" "$release/receipt.json" "$root/preview.env" <<'PY'
import json, os, sys
receipt=json.load(open(sys.argv[2]))
value={'mode':'audited-group2-same-schema-v1','operation':'entry-probe',
       'profile':receipt['account_entry'],'env_file':sys.argv[3]}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
    group2_runtime_host "$recovery/entry-request.json" "$recovery/entry.json"
  else
    compose up -d --no-build --wait --wait-timeout 180 control worker web
  fi
else
  carry_manifest=
  is_group2=false
  if [ -n "$carry_id" ]; then
    carry_manifest="$root/carry-forward/manifests/$carry_id.json"
    test -f "$carry_manifest"
    is_group2=$(python3 - "$root/carry_forward.py" "$carry_manifest" <<'PY'
import importlib.util, json, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
value=module.validate_manifest(json.load(open(sys.argv[2])))
print('true' if value.get('format_version') == module.GROUP2_FORMAT_VERSION
      and value.get('mode') == module.GROUP2_MODE else 'false')
PY
    )
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
import hashlib, importlib.util, json, pathlib, subprocess, sys
script, manifest_path, current_path, candidate_images_path, old_images_path, schema, project, controller_root = sys.argv[1:]
spec = importlib.util.spec_from_file_location('carry_forward', script)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
manifest = module.validate_manifest(json.loads(pathlib.Path(manifest_path).read_text()))
actual_controller={name:hashlib.sha256((pathlib.Path(controller_root)/name).read_bytes()).hexdigest()
                   for name in ('deploy.sh','verify.py','assets.py','carry_forward.py')}
if manifest.get('mode') == module.GROUP2_MODE:
    assert {name:manifest['review_scope']['controller_files']['files'][name]
            for name in actual_controller} == actual_controller
assert pathlib.Path(current_path).read_text().strip() == manifest['from_sha']
assert manifest['to_sha'] == pathlib.Path(candidate_images_path).parent.name
assert manifest['to_schema'] == schema
assert manifest['candidate_image_ids'] == json.loads(pathlib.Path(candidate_images_path).read_text())
assert manifest['old_image_ids'] == json.loads(pathlib.Path(old_images_path).read_text())
actual = []
containers = []
for service in ('postgres', 'rabbitmq', 'control', 'worker'):
    name = f'{project}-{service}-1'
    container_id = subprocess.check_output(
        ['docker','inspect',name,'--format','{{.Id}}'], text=True
    ).strip()
    image_id = subprocess.check_output(
        ['docker','inspect',name,'--format','{{.Image}}'], text=True
    ).strip()
    labels = {
        'com.docker.compose.project': subprocess.check_output(
            ['docker','inspect',name,'--format','{{index .Config.Labels "com.docker.compose.project"}}'], text=True
        ).strip(),
        'com.docker.compose.service': subprocess.check_output(
            ['docker','inspect',name,'--format','{{index .Config.Labels "com.docker.compose.service"}}'], text=True
        ).strip(),
    }
    container = {'service':service,'container_id':container_id,
                 'image_id':image_id,'labels':labels}
    if service == 'worker':
        raw_config = json.loads(subprocess.check_output(
            ['docker','inspect',name,'--format','{{json .Config}}'], text=True
        ))
        container['runtime_config'] = module._worker_runtime_config(raw_config)
    containers.append(container)
    mounts = json.loads(subprocess.check_output(
        ['docker','inspect',name,'--format','{{json .Mounts}}'], text=True
    ))
    actual.extend(
        {'service':service,'type':item.get('Type'),
         'source':item.get('Name') if item.get('Type') == 'volume'
                  else item.get('Source','') if item.get('Type') == 'bind' else '',
         'destination':item.get('Destination'),'read_only':not bool(item.get('RW'))}
        for item in mounts if item.get('Type') in {'volume','bind','tmpfs'}
    )
actual.sort(key=lambda item:(item['service'], item['destination']))
containers.sort(key=lambda item:item['service'])
module.validate_storage_identity(actual)
assert manifest['storage_identity'] == actual
assert manifest['old_containers'] == containers
old_images = json.loads(pathlib.Path(old_images_path).read_text())
for item in containers:
    matches = [image for tag,image in old_images.items()
               if tag.endswith(f"-{item['service']}:{manifest['from_sha']}")]
    if item['service'] != 'rabbitmq':
        assert len(matches) == 1 and matches[0] == item['image_id']
    assert item['labels'] == {'com.docker.compose.project':project,
                              'com.docker.compose.service':item['service']}
release = pathlib.Path(candidate_images_path).parent
config = json.loads(subprocess.check_output([
    'docker','compose','--project-name',project,'--env-file',str(pathlib.Path(controller_root)/'preview.env'),
    '-f',str(release/'docker-compose.yml'),'-f',str(release/'compose.preview.json'),
    'config','--format','json',
], text=True))
worker_config = config['services']['worker']
worker_environment = worker_config.get('environment', {})
assert isinstance(worker_environment, dict)
worker_images = [image for tag,image in manifest['candidate_image_ids'].items()
                 if tag.endswith(f"-worker:{manifest['to_sha']}")]
assert len(worker_images) == 1
worker_image_config = json.loads(subprocess.check_output(
    ['docker','image','inspect',worker_images[0],'--format','{{json .Config}}'], text=True
))
image_environment = dict(item.split('=',1) for item in worker_image_config.get('Env', []) if '=' in item)
image_environment.update(worker_environment)
candidate_runtime_config = module._worker_runtime_config({
    'User': worker_config.get('user') or worker_image_config.get('User') or '0',
    'Env': [f'{key}={value}' for key,value in image_environment.items()],
})
manifest_worker = [item for item in manifest['old_containers'] if item['service'] == 'worker']
assert len(manifest_worker) == 1
assert candidate_runtime_config == manifest_worker[0]['runtime_config']
candidate = []
declared = config.get('volumes', {})
for service in ('postgres', 'rabbitmq', 'control', 'worker'):
    for item in config['services'][service].get('volumes', []):
        mount_type = item.get('type')
        assert mount_type in {'volume','bind','tmpfs'}
        source = item.get('source','')
        if mount_type == 'volume':
            assert isinstance(source, str) and source in declared
            volume = declared[source]
            source = volume.get('name') or f'{project}_{source}'
        elif mount_type == 'bind':
            assert isinstance(source, str) and source.startswith('/')
        else:
            source = ''
        candidate.append({
            'service':service,'type':mount_type,'source':source,
            'destination':item.get('target'),'read_only':bool(item.get('read_only',False))
        })
candidate.sort(key=lambda item:(item['service'], item['destination']))
module.validate_storage_identity(candidate)
assert candidate == manifest['storage_identity']
PY
    carry_check="$root/carry-forward/check/$carry_id"
    install -d -m 700 "$carry_check/preflight"
    python3 - "$root/carry_forward.py" "$carry_manifest" "$carry_check/ids.json" <<'PY'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
manifest=m.validate_manifest(m.read_private(pathlib.Path(sys.argv[2])))
m.write_private(pathlib.Path(sys.argv[3]),manifest['selection'])
PY
    if [ "$is_group2" = true ]; then
      python3 - "$carry_check/preflight/account-request.json" "$project" \
        "$(cat "$root/current-sha")" "$sha" "$root" <<'PY'
import json, os, sys
path, project, old_sha, new_sha, root=sys.argv[1:]
value={'mode':'audited-group2-same-schema-v1','operation':'account-capture',
 'project':project,'from_sha':old_sha,'to_sha':new_sha,
 'old_release':f'{root}/releases/{old_sha}','candidate_release':f'{root}/releases/{new_sha}',
 'env_file':f'{root}/preview.env','old_image_ids':json.load(open(f'{root}/releases/{old_sha}/images.json')),
 'candidate_image_ids':json.load(open(f'{root}/releases/{new_sha}/images.json'))}
with open(path+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(path+'.tmp',path)
PY
      group2_runtime_host "$carry_check/preflight/account-request.json" "$carry_check/preflight/account.json"
      python3 - "$root/carry_forward.py" "$carry_manifest" "$carry_check/log-baseline.json" <<'PY'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
manifest=m.validate_manifest(m.read_private(pathlib.Path(sys.argv[2])))
m.write_private(pathlib.Path(sys.argv[3]),manifest['log_evidence'])
PY
      group2_log_checkpoint "$carry_check/log-baseline.json" "$carry_check/preflight/log.json"
      group2_log_previous="$carry_check/preflight/log.json"
      python3 - "$carry_manifest" "$carry_check/preflight/account.json" <<'PY'
import json, sys
manifest=json.load(open(sys.argv[1]))
assert json.load(open(sys.argv[2]))['account_entry'] == manifest['account_entry']
PY
    fi
    carry_state "$carry_check/preflight" --baseline /evidence/manifest.json \
      --schema-phase before \
      --db-output /evidence/db.json --files-output /evidence/files.json
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
    if ! carry_state "$carry_check/control-stopped" \
      --baseline /evidence/manifest.json \
      --schema-phase before \
      --db-output /evidence/db.json --files-output /evidence/files.json \
      > "$carry_check/control-stopped/stdout.jsonl" \
      2> "$carry_check/control-stopped/stderr.jsonl"; then
      exit 1
    fi
    if [ "$is_group2" = true ]; then
      group2_log_checkpoint "$group2_log_previous" \
        "$carry_check/control-stopped/log.json"
      group2_log_previous="$carry_check/control-stopped/log.json"
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
    if ! carry_state "$carry_check/stopped" \
      --baseline /evidence/manifest.json \
      --schema-phase before \
      --db-output /evidence/db.json --files-output /evidence/files.json \
      > "$carry_check/stopped/db-stdout.jsonl" \
      2> "$carry_check/stopped/db-stderr.jsonl"; then
      exit 1
    fi
    if [ "$is_group2" = true ]; then
      group2_log_checkpoint "$group2_log_previous" \
        "$carry_check/stopped/log.json"
      group2_log_previous="$carry_check/stopped/log.json"
    fi
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
    carry_state "$carry_check/after-backup" --baseline /evidence/manifest.json \
      --schema-phase before \
      --db-output /evidence/db.json --files-output /evidence/files.json
    if [ "$is_group2" = true ]; then
      group2_log_checkpoint "$group2_log_previous" \
        "$carry_check/after-backup/log.json"
      group2_log_previous="$carry_check/after-backup/log.json"
    fi
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
    carry_state "$carry_check/after-migration" --baseline /evidence/manifest.json \
      --schema-phase after \
      --db-output /evidence/db.json --files-output /evidence/files.json
    if [ "$is_group2" = true ]; then
      group2_log_checkpoint "$group2_log_previous" \
        "$carry_check/after-migration/log.json"
      group2_log_previous="$carry_check/after-migration/log.json"
    fi
    python3 "$root/carry_forward.py" check-kernel \
      --unit "$sandbox_unit" --expected-description "$sandbox_description" \
      --require-idle --baseline "$carry_manifest" \
      --output "$carry_check/after-migration/kernel.json"
  fi
  if [ "$is_group2" = true ]; then
    record starting
    group2="$carry_check/group2"
    install -d -m 700 "$group2"
    install -m 600 "$group2_log_previous" "$group2/log-before-start.json"
    python3 - "$group2/start-window.json" <<'PY'
import json, os, sys, time
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump({'window_start_ns':time.time_ns()},out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  fi
  if [ "$is_group2" = true ]; then
    compose up -d --no-build --wait --wait-timeout 180 control worker web account-web
  else
    compose up -d --no-build --wait --wait-timeout 180 control worker web
  fi
fi
curl --fail --silent "http://127.0.0.1:$web_port/api/health"
worker_id=$(compose ps -q worker)
test "$(docker inspect "$worker_id" --format '{{.HostConfig.CgroupnsMode}}:{{.HostConfig.Privileged}}')" = private:false
compose exec -T worker python -c 'import os; assert os.path.exists("/tmp/dlr-worker.ready"); assert open("/proc/1/cgroup").read().strip() in {"0::/", "0::/agent"}; assert os.path.isdir("/run/dlr-cgroup/agent"); assert open("/run/dlr-cgroup/memory.max").read().strip() != "max"; assert open("/run/dlr-cgroup/cpu.max").read().split()[0] != "max"'
# Verify the running containers use the exact recorded images.
for service in postgres control worker web; do
  test "$(docker inspect "$project-$service-1" --format '{{.Image}}')" = "$(docker image inspect "$project-$service:$sha" --format '{{.Id}}')"
done
if [ "${is_group2:-false}" = true ]; then
  test "$(docker inspect "$project-account-web-1" --format '{{.Image}}')" = "$(docker image inspect "$project-web:$sha" --format '{{.Id}}')"
fi
if [ "$action" = recover ] && [ "${is_group2:-false}" = true ]; then
  python3 - "$root/carry_forward.py" "$carry_manifest" \
    "$release/recovery-baseline" "$recovery" "$recovery_id" "$sha" <<'PY'
import importlib.util, json, pathlib, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
manifest=m.validate_manifest(m.read_private(pathlib.Path(sys.argv[2])))
baseline_path=pathlib.Path(sys.argv[3]); evidence_path=pathlib.Path(sys.argv[4])
def read(path): return json.load(open(path))
baseline=read(evidence_path/'baseline.json')
account=read(evidence_path/'account.json'); entry=read(evidence_path/'entry.json')
evidence={
 'request':read(evidence_path/'startup-request.json'),
 'proof':read(evidence_path/'startup.json')['startup_proof'],
 'before_db':read(evidence_path/'before-db.json'),
 'after_db':read(evidence_path/'after-db.json'),
 'before_files':read(evidence_path/'before-files.json'),
 'after_files':read(evidence_path/'after-files.json'),
 'account_check':account['account_check'],'entry_probe':entry['entry_probe'],
 'logs_before':read(evidence_path/'log-before.json')['log_evidence'],
 'logs_after':read(evidence_path/'log-after.json')['log_evidence'],
 'preservation':read(evidence_path/'preservation.json'),
}
result=m.validate_group2_recovery_evidence(
    manifest,baseline,evidence,baseline['deployment']
)
evidence_digest=m.digest({'baseline':baseline,'evidence':evidence})
completion={'schema':'group2-recovery-completion-v1','recovery_id':sys.argv[5],
 'sha':sys.argv[6],'manifest_id':manifest['manifest_id'],
 'manifest_digest':manifest['manifest_digest'],'evidence_digest':evidence_digest,
 'predecessor':{'kind':baseline['predecessor']['kind'],
                'recovery_id':baseline['predecessor']['recovery_id'],
                'evidence_digest':baseline['predecessor']['evidence_digest']},
 'result':result}
m.write_private(evidence_path/'completion.json',completion)
print(evidence_digest)
PY
  recovery_completion_digest=$(python3 - "$recovery/completion.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))['evidence_digest'])
PY
  )
  record ready
fi
if [ "$action" = deploy ] && [ "${is_group2:-false}" = true ]; then
  python3 - "$group2/start-window.json" <<'PY'
import json, os, sys, time
path=sys.argv[1]; value=json.load(open(path)); value['window_end_ns']=time.time_ns()
with open(path+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(path+'.tmp',path)
PY
  python3 - "$group2/account-check-request.json" "$carry_manifest" "$project" "$sha" "$release/images.json" <<'PY'
import json, os, sys
manifest=json.load(open(sys.argv[2]))
value={'mode':'audited-group2-same-schema-v1','operation':'account-check',
       'profile':manifest['account_entry'],'project':sys.argv[3],
       'to_sha':sys.argv[4],'candidate_image_ids':json.load(open(sys.argv[5]))}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/account-check-request.json" "$group2/account-check.json"
  python3 - "$group2/log-after-start-request.json" "$group2/log-before-start.json" <<'PY'
import json, os, sys
value={'mode':'audited-group2-same-schema-v1','operation':'log-append',
       'baseline':json.load(open(sys.argv[2]))['log_evidence']}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/log-after-start-request.json" "$group2/log-after-start.json"
  python3 - "$group2/startup-request.json" "$carry_manifest" \
    "$group2/log-before-start.json" "$group2/log-after-start.json" \
    "$group2/account-check.json" "$group2/start-window.json" <<'PY'
import json, os, sys
manifest=json.load(open(sys.argv[2])); window=json.load(open(sys.argv[6]))
old=manifest['account_entry']['old_containers']
if isinstance(old,list): old={item['service']:item for item in old}
checked=json.load(open(sys.argv[5]))
value={'mode':'audited-group2-same-schema-v1','operation':'startup-proof',
       'profile':manifest['account_entry'],
       'logs_before':json.load(open(sys.argv[3]))['log_evidence'],
       'logs_after':json.load(open(sys.argv[4]))['log_evidence'],
       'container_before':old['worker'],
       'container_after':checked['account_check']['containers']['worker'], **window}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/startup-request.json" "$group2/startup.json"
  install -d -m 700 "$group2/started"
  install -m 600 "$carry_check/ids.json" "$group2/started/ids.json"
  carry_state "$group2/started" --ids /evidence/ids.json \
    --mode audited-group2-same-schema-v1 \
    --db-output /evidence/db.json --files-output /evidence/files.json
  python3 - "$root/carry_forward.py" "$carry_manifest" \
    "$group2/started/db.json" "$group2/started/files.json" \
    "$group2/startup.json" "$group2/started-check.json" <<'PY'
import importlib.util, json, pathlib, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
manifest=json.load(open(sys.argv[2])); after=json.load(open(sys.argv[3])); files=json.load(open(sys.argv[4])); proof=json.load(open(sys.argv[5]))['startup_proof']
m.compare_projection(manifest['old_runtime_projection'],after['projection'])
for key in ('responsibilities','protected_rows','asset_projection','schema_shape'):
    if manifest[key] != after[key]: raise m.CarryForwardError('group2_started_db_changed')
result=m.compare_group2_startup_files(manifest['file_evidence'],files,proof)
m.write_private(pathlib.Path(sys.argv[6]),result)
PY
  python3 - "$group2/entry-request.json" "$carry_manifest" "$root/preview.env" <<'PY'
import json, os, sys
manifest=json.load(open(sys.argv[2]))
value={'mode':'audited-group2-same-schema-v1','operation':'entry-probe',
       'profile':manifest['account_entry'],'env_file':sys.argv[3]}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/entry-request.json" "$group2/entry-before.json"
  group2_log_checkpoint "$group2/log-after-start.json" \
    "$group2/log-before-probe.json"
  record verifying
  compose exec -T control python - < "$root/verify.py" > "$group2/probe.pending.json"
  python3 - "$group2/probe.pending.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1]))
assert set(value)=={'status','workspace_cleanup_status','execution_id'}
assert value['status']=='succeeded' and value['workspace_cleanup_status']=='completed'
assert type(value['execution_id']) is int and value['execution_id'] > 0
PY
  python3 - "$group2/log-partial-request.json" "$group2/log-before-probe.json" <<'PY'
import json, os, sys
value={'mode':'audited-group2-same-schema-v1','operation':'log-append',
       'baseline':json.load(open(sys.argv[2]))['log_evidence']}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/log-partial-request.json" "$group2/log-partial.json"
  python3 - "$group2/probe-partial-request.json" "$group2/log-before-probe.json" \
    "$group2/log-partial.json" "$group2/probe.pending.json" "$group2/started/db.json" <<'PY'
import json, os, sys
value={'mode':'audited-group2-same-schema-v1','operation':'probe-proof',
       'logs_before':json.load(open(sys.argv[2]))['log_evidence'],
       'logs_after':json.load(open(sys.argv[3]))['log_evidence'],
       'probe_result':json.load(open(sys.argv[4])),
       'before_db':json.load(open(sys.argv[5])),'cleanup':None}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/probe-partial-request.json" "$group2/probe-partial.json"
  python3 - "$group2/cleanup-request.json" "$group2/probe-partial.json" "$group2/started/db.json" <<'PY'
import json, os, sys
value={'mode':'audited-group2-same-schema-v1','operation':'probe-cleanup',
       'provenance':json.load(open(sys.argv[2]))['probe_proof'],
       'before_db':json.load(open(sys.argv[3])),'timeout_seconds':120}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  carry_control "$group2" group2-runtime \
    --request /evidence/cleanup-request.json --output /evidence/cleanup.json >/dev/null
  python3 - "$group2/log-final-request.json" "$group2/log-partial.json" <<'PY'
import json, os, sys
value={'mode':'audited-group2-same-schema-v1','operation':'log-append',
       'baseline':json.load(open(sys.argv[2]))['log_evidence']}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/log-final-request.json" "$group2/log-final.json"
  python3 - "$root/carry_forward.py" "$group2/log-before-probe.json" \
    "$group2/log-partial.json" "$group2/log-final.json" \
    "$group2/log-complete.json" <<'PY'
import importlib.util, json, pathlib, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
values=[json.load(open(path))['log_evidence'] for path in sys.argv[2:5]]
m.write_private(pathlib.Path(sys.argv[5]),
                {'log_evidence':m.combine_group2_log_window(*values)})
PY
  python3 - "$group2/probe-final-request.json" "$group2/log-before-probe.json" \
    "$group2/log-complete.json" "$group2/probe.pending.json" \
    "$group2/started/db.json" "$group2/cleanup.json" <<'PY'
import json, os, sys
value={'mode':'audited-group2-same-schema-v1','operation':'probe-proof',
       'logs_before':json.load(open(sys.argv[2]))['log_evidence'],
       'logs_after':json.load(open(sys.argv[3]))['log_evidence'],
       'probe_result':json.load(open(sys.argv[4])),
       'before_db':json.load(open(sys.argv[5])),
       'cleanup':json.load(open(sys.argv[6]))['cleanup']}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/probe-final-request.json" "$group2/probe-final.json"
  install -d -m 700 "$group2/final"
  install -m 600 "$carry_check/ids.json" "$group2/final/ids.json"
  carry_state "$group2/final" --ids /evidence/ids.json \
    --mode audited-group2-same-schema-v1 \
    --db-output /evidence/db.json --files-output /evidence/files.json
  python3 - "$root/carry_forward.py" "$carry_manifest" "$group2/started/db.json" \
    "$group2/final/db.json" "$group2/started/files.json" "$group2/final/files.json" \
    "$group2/probe-final.json" "$group2/post-preservation.json" <<'PY'
import importlib.util, json, pathlib, sys
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
objects=[json.load(open(path)) for path in sys.argv[2:8]]
result=m.compare_group2_post_probe(objects[0],objects[1],objects[2],objects[3],objects[4],objects[5]['probe_proof'])
m.write_private(pathlib.Path(sys.argv[8]),result)
PY
  install -m 600 "$group2/log-final.json" "$group2/log-before-health.json"
  group2_runtime_host "$group2/account-check-request.json" "$group2/account-after.json"
  group2_runtime_host "$group2/entry-request.json" "$group2/entry-after.json"
  python3 - "$group2/log-after-health-request.json" "$group2/log-before-health.json" <<'PY'
import json, os, sys
value={'mode':'audited-group2-same-schema-v1','operation':'log-append',
       'baseline':json.load(open(sys.argv[2]))['log_evidence']}
with open(sys.argv[1]+'.tmp','w') as out:
    json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
PY
  group2_runtime_host "$group2/log-after-health-request.json" "$group2/log-after-health.json"
  record committing
  printf '%s\n' "$schema" > "$release/schema"
  mv "$group2/probe.pending.json" "$release/probe.json"
  install -d -m 700 "$release/recovery-baseline"
  install -m 600 "$carry_check/ids.json" "$release/recovery-baseline/ids.json"
  install -m 600 "$group2/final/db.json" "$release/recovery-baseline/db.json"
  install -m 600 "$group2/final/files.json" "$release/recovery-baseline/files.json"
  install -m 600 "$group2/post-preservation.json" "$release/recovery-baseline/post-preservation.json"
  install -m 600 "$group2/log-after-health.json" "$release/recovery-baseline/logs-after.json"
  python3 - "$release" "$backup" "$carry_manifest" "$group2" \
    "$root/carry_forward.py" <<'PY'
import hashlib, importlib.util, json, os, pathlib, sys
release=pathlib.Path(sys.argv[1]); evidence=pathlib.Path(sys.argv[4]); manifest=json.load(open(sys.argv[3]))
spec=importlib.util.spec_from_file_location('carry_forward',sys.argv[5])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def read(name): return json.load(open(evidence/name))
post_bundle={'result':read('post-preservation.json'),'db':read('final/db.json'),'files':read('final/files.json')}
raw_stages={
 'preflight':json.load(open(evidence.parent/'preflight/db.json')),
 'control_stopped':json.load(open(evidence.parent/'control-stopped/db.json')),
 'stopped':json.load(open(evidence.parent/'stopped/db.json')),
 'backup':{'db':json.load(open(evidence.parent/'after-backup/db.json')),
           'dump_sha256':hashlib.sha256((pathlib.Path(sys.argv[2])/'database.dump').read_bytes()).hexdigest(),
           'list_sha256':hashlib.sha256((pathlib.Path(sys.argv[2])/'database.list').read_bytes()).hexdigest()},
 'same_schema':json.load(open(evidence.parent/'after-migration/db.json')),
 'started':read('started-check.json'),
 'probe':read('probe-final.json')['probe_proof']['probe_result'],
 'natural_cleanup':read('cleanup.json')['cleanup'],
 'post_preservation':post_bundle,
 'post_health':{'account_check':read('account-after.json')['account_check'],
                'entry_probe':read('entry-after.json')['entry_probe'],
                'logs_after':read('log-after-health.json')['log_evidence']},
}
stage_paths={'preflight':'preflight','control_stopped':'control-stopped',
 'stopped':'stopped','after_backup':'after-backup','after_migration':'after-migration'}
stage_inputs={name:{'db':json.load(open(evidence.parent/path/'db.json')),
                    'files':json.load(open(evidence.parent/path/'files.json')),
                    'logs_after':json.load(open(evidence.parent/path/'log.json'))['log_evidence']}
              for name,path in stage_paths.items()}
startup={'request':read('startup-request.json'),'proof':read('startup.json')['startup_proof'],
 'before_files':manifest['file_evidence'],'after_files':read('started/files.json'),
 'after_db':read('started/db.json'),'result':read('started-check.json')}
probe={'proof':read('probe-final.json')['probe_proof'],'before_db':read('started/db.json'),
 'after_db':read('final/db.json'),'before_files':read('started/files.json'),
 'after_files':read('final/files.json'),'logs_before':read('log-before-probe.json')['log_evidence'],
 'logs_partial':read('log-partial.json')['log_evidence'],'logs_final':read('log-final.json')['log_evidence'],
 'logs_complete':read('log-complete.json')['log_evidence'],'probe_result':raw_stages['probe'],
 'cleanup':raw_stages['natural_cleanup'],'result':read('post-preservation.json')}
post_health={'account_check':raw_stages['post_health']['account_check'],
 'entry_probe':raw_stages['post_health']['entry_probe'],
 'logs_before':read('log-before-health.json')['log_evidence'],
 'logs_after':raw_stages['post_health']['logs_after']}
receipt_evidence={'stages':raw_stages,'stage_inputs':stage_inputs,'startup':startup,
                  'probe':probe,'post_health':post_health}
m.validate_group2_receipt_evidence(manifest,receipt_evidence)
stages={name:digest(value) for name,value in raw_stages.items()}
account=manifest['account_entry']; post=read('post-preservation.json')
value={'mode':manifest['mode'],'sha':manifest['to_sha'],'schema':(release/'schema').read_text().strip(),
 'images':json.load(open(release/'images.json')),'probe':json.load(open(release/'probe.json')),
 'backup':sys.argv[2], 'carry_forward':{'manifest_id':manifest['manifest_id'],'manifest_digest':manifest['manifest_digest']},
 'ci_binding':manifest['ci_binding'],'review_scope_digest':manifest['review_scope_digest'],
 'stages':stages,'post_preservation_digest':digest(post_bundle),
 'evidence_digest':digest(receipt_evidence),
 'account_entry_digest':account['profile_digest'],'account_entry':account,'account_ready':True}
temporary=release/'receipt.json.tmp'
with temporary.open('w') as out: json.dump(value,out); out.flush(); os.fsync(out.fileno())
os.replace(temporary,release/'receipt.json')
PY
  printf '%s\n' "$sha" > "$root/current-sha.tmp"
  mv "$root/current-sha.tmp" "$root/current-sha"
  record ready
  compose ps
  exit 0
fi
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
