#!/usr/bin/env bash
# Trusted controller, executed only in the dedicated Colima VM.
set -euo pipefail
umask 077
sha=${1:?commit required}
action=${2:?stage, deploy, recover or adopt required}
schema=${3:-}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$action" =~ ^(stage|deploy|recover|adopt)$ ]] || exit 2
root=$(cd "$(dirname "$0")" && pwd)
deployment_settings=$(python3 - "$root/deployment.json" <<'PYSETTINGS'
import json, shlex, sys
with open(sys.argv[1]) as source: settings = json.load(source)
for key in ('project', 'web_port', 'sandbox_unit', 'sandbox_cpu_quota', 'sandbox_memory_max'):
    print(key + '=' + shlex.quote(str(settings[key])))
PYSETTINGS
)
eval "$deployment_settings"
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
  python3 - "$root/transaction.json" "$1" "$sha" "${backup:-}" <<'PY'
import json, os, sys, time
path, phase, sha, backup = sys.argv[1:]
with open(path + '.tmp', 'w') as out:
    json.dump({'phase': phase, 'sha': sha, 'backup': backup, 'at': time.time()}, out)
    out.flush(); os.fsync(out.fileno())
os.replace(path + '.tmp', path)
PY
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
bash "$root/prepare-sandbox-host.sh" --unit "$sandbox_unit" --cpu-quota "$sandbox_cpu_quota" --memory-max "$sandbox_memory_max"
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
  # Delay while queued/running/retrying work or unfinished cleanup exists.
  busy_query="SELECT (SELECT count(*) FROM executions WHERE status IN ('queued','running','retry_wait') OR workspace_cleanup_status='pending') + (SELECT count(*) FROM execution_attempts WHERE status IN ('claimed','running'))"
  test "$(sql "$busy_query")" = 0 || exit 75
  old=$(cat "$root/current-sha")
  old_compose() { docker compose --project-name "$project" --env-file "$root/preview.env" -f "$root/releases/$old/docker-compose.yml" -f "$root/releases/$old/compose.preview.json" "$@"; }
  record quiescing
  # This stops API, schedules, retries and dispatch creation before the second check.
  old_compose stop control
  if [ "$(sql "$busy_query")" != 0 ]; then
    old_compose up -d --no-build --wait --wait-timeout 180 control
    python3 - "$root/transaction.json" "$old" <<'PY'
import json, sys
with open(sys.argv[1], 'w') as out: json.dump({'phase':'ready','sha':sys.argv[2]},out)
PY
    exit 75
  fi
  old_compose stop worker web account-web
  backup="$root/backups/$(date -u +%Y%m%dT%H%M%SZ)-$old-to-$sha"
  mkdir -p "$backup"
  record backing_up
  docker exec "$project-postgres-1" pg_dump -U dlr -d dlr -Fc > "$backup/database.dump"
  docker exec -i "$project-postgres-1" pg_restore --list < "$backup/database.dump" > "$backup/database.list"
  test -s "$backup/database.list"
  compose run --rm --no-deps -T -v "$root:/preview:ro" control python /preview/assets.py > "$backup/assets.json"
  compose up -d --no-build --wait --wait-timeout 180 postgres rabbitmq
  record migrating
  compose run --rm --no-deps -T control alembic upgrade head
  test "$(sql 'SELECT version_num FROM alembic_version')" = "$schema"
  compose run --rm --no-deps -T -v "$root:/preview:ro" control python /preview/assets.py "/preview/backups/$(basename "$backup")/assets.json" > "$backup/assets-after.json"
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
  python3 - "$release" "$backup" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
with (root / 'receipt.json').open('w') as out:
    json.dump({'schema': (root/'schema').read_text().strip(), 'images': json.loads((root/'images.json').read_text()),
               'probe': json.loads((root/'probe.json').read_text()), 'backup':sys.argv[2]}, out)
PY
  printf '%s\n' "$sha" > "$root/current-sha.tmp"
  mv "$root/current-sha.tmp" "$root/current-sha"
  record ready
fi
compose ps
