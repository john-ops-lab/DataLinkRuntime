#!/usr/bin/env bash
# Cross-check the platform-log configuration examples and documentation.
set -euo pipefail

cd "$(dirname "$0")/.."

DOCS=(.env.example README.md README.en.md docs/deployment/platform-logs.md)
LOG_DIRS=(control/ worker/ web/ account-web/ postgres/)

contains_literal() {
  local file="$1"
  local text="$2"
  if command -v rg >/dev/null 2>&1; then
    rg --fixed-strings --quiet -- "$text" "$file"
  else
    grep -Fq -- "$text" "$file"
  fi
}

contains_pattern() {
  local file="$1"
  local pattern="$2"
  if command -v rg >/dev/null 2>&1; then
    rg --quiet -- "$pattern" "$file"
  else
    grep -Eq -- "$pattern" "$file"
  fi
}

matching_lines() {
  local pattern="$1"
  shift
  if command -v rg >/dev/null 2>&1; then
    rg -n -i -- "$pattern" "$@"
  else
    grep -Eni -- "$pattern" "$@"
  fi
}

require_literal() {
  local file="$1"
  local text="$2"
  if ! contains_literal "$file" "$text"; then
    echo "Missing expected text in $file: $text" >&2
    exit 1
  fi
}

require_pattern() {
  local file="$1"
  local pattern="$2"
  if ! contains_pattern "$file" "$pattern"; then
    echo "Missing expected pattern in $file: $pattern" >&2
    exit 1
  fi
}

for doc in "${DOCS[@]}"; do
  test -f "$doc"
  require_literal "$doc" "DLR_PLATFORM_LOG_ROOT"
  require_literal "$doc" "./platform-logs"
  require_literal "$doc" "/var/lib/dlr/platform-logs"
  for log_dir in "${LOG_DIRS[@]}"; do
    require_literal "$doc" "$log_dir"
  done
  require_pattern "$doc" '(postgres|PostgreSQL).{0,120}(container user|容器内|writable|可写|用户)'
  require_pattern "$doc" '(bind mount|bind-mounted)'
  require_pattern "$doc" '(rotation|轮转)'
  require_pattern "$doc" '(redaction|脱敏)'
  require_pattern "$doc" '(credential|凭据)'
  require_pattern "$doc" '(Do not use.*chmod 777|不要使用.*chmod 777)'
done

for doc in "${DOCS[@]}"; do
  require_literal "$doc" "DLR_AI_ASSIST_TOTAL_TIMEOUT_SECONDS"
  require_literal "$doc" "DLR_AI_TOOL_AUDIT_MAX_BYTES"
  require_literal "$doc" "DLR_AI_TOOL_AUDIT_BACKUP_COUNT"
  require_literal "$doc" "ai-tool-audit.jsonl"
  require_literal "$doc" "*.log"
  require_pattern "$doc" '110[[:space:]]*MiB'
  require_pattern "$doc" '(回滚|rollback)'
done

require_literal .env.example 'DLR_AI_ASSIST_TOTAL_TIMEOUT_SECONDS=150'
require_literal .env.example 'DLR_AI_TOOL_AUDIT_MAX_BYTES=10485760'
require_literal .env.example 'DLR_AI_TOOL_AUDIT_BACKUP_COUNT=10'

if contains_pattern .env.example '(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})'; then
  echo "The .env.example file must not contain credential-shaped values" >&2
  exit 1
fi

if contains_pattern .env.example '^DLR_PLATFORM_LOG_ROOT=/var/lib/dlr/platform-logs'; then
  echo "The .env.example active value must remain the local writable path" >&2
  exit 1
fi
require_literal .env.example 'DLR_PLATFORM_LOG_ROOT=./platform-logs'

if ! contains_pattern .gitignore '^/platform-logs/$'; then
  echo "Missing exact root platform-log ignore rule in .gitignore" >&2
  exit 1
fi
require_literal README.md '已在 `.gitignore` 中精确忽略'

for log_dir in "${LOG_DIRS[@]}"; do
  service_dir="${log_dir%/}"
  require_literal docker-compose.yml "/${service_dir}:/var/lib/dlr/platform-logs/${service_dir}"
done

require_literal .github/workflows/ci.yml '- name: Check platform-log documentation'
require_literal .github/workflows/ci.yml 'run: ./scripts/check-platform-log-docs.sh'

# A chmod 777 command must never be presented as an executable recommendation.
while IFS= read -r line; do
  if ! printf '%s\n' "$line" | grep -Eqi '(Do not use|不要使用)'; then
    echo "Unsafe chmod 777 recommendation found: $line" >&2
    exit 1
  fi
done < <(matching_lines 'chmod[[:space:]]+0?777' "${DOCS[@]}" || true)

python3 - <<'PY'
import re
from pathlib import Path
from urllib.parse import urlsplit

root = Path.cwd()
for markdown in (
    root / "README.md",
    root / "README.en.md",
    root / "docs/deployment/platform-logs.md",
):
    text = markdown.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        target = target.strip().split()[0].strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or target.startswith(("//", "#")):
            continue
        relative = parsed.path.split("#", 1)[0]
        if not relative:
            continue
        resolved = (markdown.parent / relative).resolve()
        if root not in resolved.parents and resolved != root:
            raise SystemExit(f"Markdown link escapes repository: {markdown}: {target}")
        if not resolved.exists():
            raise SystemExit(f"Broken relative Markdown link: {markdown}: {target}")
PY

echo "platform-log documentation consistency: PASS"
echo "checked: ${DOCS[*]}"
echo "host roots: ./platform-logs (local), /var/lib/dlr/platform-logs (Linux production)"
echo "directories: ${LOG_DIRS[*]}"
echo "compose bind mounts, safe chmod guidance, and relative Markdown links: PASS"
