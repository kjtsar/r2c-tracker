#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "${SCRIPT_DIR}"

usage() {
  echo "Usage: ./publish_release.sh --bypass-safety-checks [--allow-non-presentation-changes] APP_VERSION_CODE" >&2
  echo "This fast path skips the idle gate and isolated hosted staging." >&2
  exit 2
}

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ] || [ "$1" != "--bypass-safety-checks" ]; then
  usage
fi

allow_non_presentation_changes=0
if [ "$#" -eq 3 ]; then
  [ "$2" = "--allow-non-presentation-changes" ] || usage
  allow_non_presentation_changes=1
  app_version_code="$3"
else
  app_version_code="$2"
fi

case "${app_version_code}" in
  ''|*[!0-9]*|0) usage ;;
esac

echo "WARNING: --bypass-safety-checks is not a production release qualification."
echo "It may disconnect active tracker clients and is restricted to presentation-only changes."
if [ "${allow_non_presentation_changes}" = "1" ]; then
  echo "WARNING: explicitly allowing qualified backend/deployment changes without hosted staging."
fi

if .venv/bin/python scripts/release_guard.py qualification-current; then
  echo "Reusing the full qualification receipt for this exact clean commit."
else
  ./qualify_release.sh
fi
if [ "${allow_non_presentation_changes}" = "1" ]; then
  ./deploy_candidate.sh "${app_version_code}" --bypass-safety-checks --allow-non-presentation-changes
else
  ./deploy_candidate.sh "${app_version_code}" --bypass-safety-checks
fi
./promote_candidate.sh --bypass-safety-checks
