#!/bin/sh
set -eu

PROJECT_ID="${PROJECT_ID:-shaped-splicer-482602-v1}"
DB_VM_NAME="${DB_VM_NAME:-instance-20260104-171736}"
DB_VM_ZONE="${DB_VM_ZONE:-us-west1-b}"
OUTPUT_DIR="${SECURITY_OUTPUT_DIR:-security-artifacts/cloud}"

mkdir -p "${OUTPUT_DIR}"

gcloud compute instances describe "${DB_VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${DB_VM_ZONE}" \
  --format=json > "${OUTPUT_DIR}/database-vm.json"

gcloud compute firewall-rules list \
  --project="${PROJECT_ID}" \
  --format=json > "${OUTPUT_DIR}/firewall-rules.json"

gcloud projects get-iam-policy "${PROJECT_ID}" \
  --format=json > "${OUTPUT_DIR}/project-iam.json"

gcloud alpha monitoring policies list \
  --project="${PROJECT_ID}" \
  --format=json > "${OUTPUT_DIR}/alert-policies.json"

gcloud alpha monitoring channels list \
  --project="${PROJECT_ID}" \
  --format=json > "${OUTPUT_DIR}/notification-channels.json"

gcloud logging metrics list \
  --project="${PROJECT_ID}" \
  --format=json > "${OUTPUT_DIR}/log-metrics.json"

echo "Cloud security inventory written to ${OUTPUT_DIR}."
echo "Review and sanitize it before sharing; IAM exports contain account identifiers."
