#! /bin/sh

set -eu

: "${SCW_SECRET_KEY?SCW_SECRET_KEY environment variable must be set}"

exec python /scw_registry_cleaner/cli.py "$@"
