#!/bin/sh

set -eu pipefail

: "${SCW_SECRET_KEY?SCW_SECRET_KEY environment variable must be set}"

python ./scw_registry_cleaner/cli.py "$@"
