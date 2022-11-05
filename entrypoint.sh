#! /bin/sh

set -eu

exec python /app/scw_registry_cleaner/cli.py "$@"
