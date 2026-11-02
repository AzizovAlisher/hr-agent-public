#!/bin/sh
set -eu

: "${MINIO_ROOT_USER:?MINIO_ROOT_USER is required}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is required}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY is required}"

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing local/hr-agent-private
mc mb --ignore-existing local/hr-agent-public
mc version enable local/hr-agent-private
mc version enable local/hr-agent-public

mc admin user add local "$S3_ACCESS_KEY" "$S3_SECRET_KEY"
mc admin policy create local hr-agent-runtime /policies/runtime-policy.json
mc admin policy attach local hr-agent-runtime --user "$S3_ACCESS_KEY"

# Anonymous access is applied only to the public bucket and only its logos
# prefix. The private bucket never receives an anonymous policy.
mc anonymous set none local/hr-agent-private
mc anonymous set-json /policies/public-read-policy.json local/hr-agent-public

# Lifecycle is defense in depth. Authorized immediate erasure is performed by
# the application with ListBucketVersions + DeleteObjectVersion and verified
# before its durable outbox settles. These deterministic policies bound stale
# quarantine objects and noncurrent versions if a worker remains unavailable.
mc ilm rule import local/hr-agent-private < /policies/private-lifecycle.json
mc ilm rule import local/hr-agent-public < /policies/public-lifecycle.json
