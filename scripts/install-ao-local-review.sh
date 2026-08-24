#!/usr/bin/env bash
set -euo pipefail

ROBOREV_VERSION="0.66.0"
AO_DATA_ROOT="${AO_DATA_DIR:-${HOME}/.ao/data}"
INSTALL_ROOT="${AO_DATA_ROOT}/local-review"
USER_BIN="${HOME}/.local/bin"
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "Usage: $0 [--install-root PATH] [--user-bin PATH]" >&2
}

while (($#)); do
  case "$1" in
    --install-root)
      INSTALL_ROOT="$2"
      shift 2
      ;;
    --user-bin)
      USER_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)
    ASSET="roborev_${ROBOREV_VERSION}_darwin_arm64.tar.gz"
    EXPECTED_SHA256="eb6e68b2a1a86343f6147045ab124b7b49496c3c17afbea4b70990280b9817d7"
    ;;
  Darwin-x86_64)
    ASSET="roborev_${ROBOREV_VERSION}_darwin_amd64.tar.gz"
    EXPECTED_SHA256="99a1a4c4a9782426cf64810beaf8929281541fc574825849233015e2dcf09f45"
    ;;
  Linux-x86_64)
    ASSET="roborev_${ROBOREV_VERSION}_linux_amd64.tar.gz"
    EXPECTED_SHA256="3a57bda163559cf9b9062a3808c0979c4b46853ff712681d2160d676e2ea098a"
    ;;
  Linux-aarch64|Linux-arm64)
    ASSET="roborev_${ROBOREV_VERSION}_linux_arm64.tar.gz"
    EXPECTED_SHA256="371accc17a7afa1e38f10ac7e2e0540681cdcd5d73bdfb670e51cc24d225862f"
    ;;
  *)
    echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2
    exit 2
    ;;
esac

mkdir -p "${INSTALL_ROOT}/bin" "${INSTALL_ROOT}/roborev" "${USER_BIN}"
chmod 700 "${INSTALL_ROOT}" "${INSTALL_ROOT}/bin" "${INSTALL_ROOT}/roborev"

install -m 0755 "${SOURCE_ROOT}/ao-local-review" "${INSTALL_ROOT}/bin/ao-local-review"
install -m 0644 "${SOURCE_ROOT}/ao_local_review.py" "${INSTALL_ROOT}/bin/ao_local_review.py"
install -m 0755 "${SOURCE_ROOT}/ao-local-review-claude-wrapper" "${INSTALL_ROOT}/bin/ao-local-review-claude-wrapper"
install -m 0644 "${SOURCE_ROOT}/ao_local_review_claude_wrapper.py" "${INSTALL_ROOT}/bin/ao_local_review_claude_wrapper.py"

CURRENT_VERSION=""
if [[ -x "${INSTALL_ROOT}/bin/roborev" ]]; then
  CURRENT_VERSION="$("${INSTALL_ROOT}/bin/roborev" version 2>/dev/null | awk '{print $2}' | sed 's/^v//')"
fi

if [[ "${CURRENT_VERSION}" != "${ROBOREV_VERSION}" ]]; then
  TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ao-local-review-install.XXXXXX")"
  trap 'rm -rf "${TEMP_DIR}"' EXIT
  URL="https://github.com/kenn-io/roborev/releases/download/v${ROBOREV_VERSION}/${ASSET}"
  curl --fail --location --silent --show-error "${URL}" --output "${TEMP_DIR}/${ASSET}"
  ACTUAL_SHA256="$(shasum -a 256 "${TEMP_DIR}/${ASSET}" | awk '{print $1}')"
  if [[ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]]; then
    echo "RoboRev checksum mismatch: expected ${EXPECTED_SHA256}, got ${ACTUAL_SHA256}" >&2
    exit 1
  fi
  tar -xzf "${TEMP_DIR}/${ASSET}" -C "${TEMP_DIR}"
  install -m 0755 "${TEMP_DIR}/roborev" "${INSTALL_ROOT}/bin/roborev"
fi

if [[ -e "${USER_BIN}/ao-local-review" && ! -L "${USER_BIN}/ao-local-review" ]]; then
  echo "Refusing to replace non-symlink command: ${USER_BIN}/ao-local-review" >&2
  exit 1
fi
ln -sfn "${INSTALL_ROOT}/bin/ao-local-review" "${USER_BIN}/ao-local-review"

"${INSTALL_ROOT}/bin/roborev" version
"${INSTALL_ROOT}/bin/ao-local-review" --help >/dev/null
echo "Installed ao-local-review: ${USER_BIN}/ao-local-review"
echo "Managed root: ${INSTALL_ROOT}"
echo "No Git hooks, RoboRev skills, fix/refine flows, or agent hooks were installed."
