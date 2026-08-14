#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "${SCRIPT_DIR}"

usage() {
  echo "Usage: ./publish_release.sh --bypass-safety-checks APP_VERSION_CODE" >&2
  echo "This presentation-only path skips the idle gate and isolated hosted staging." >&2
  exit 2
}

if [ "$#" -ne 2 ] || [ "$1" != "--bypass-safety-checks" ]; then
  usage
fi

case "$2" in
  ''|*[!0-9]*|0) usage ;;
esac

app_version_code="$2"

echo "WARNING: --bypass-safety-checks is not a production release qualification."
echo "It may disconnect active tracker clients and is restricted to presentation-only changes."

./qualify_release.sh
./deploy_candidate.sh "${app_version_code}" --bypass-safety-checks
./test_candidate.sh
./promote_candidate.sh --bypass-safety-checks
