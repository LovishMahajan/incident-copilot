#!/bin/sh
# Builds the sandbox image and prints its ID (sha256:...), the value for AISRE_SANDBOX_IMAGE.
set -eu
exec docker build --quiet --label aisre.harness.image=1 "$(dirname "$0")"
