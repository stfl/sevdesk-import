# Recipes assume their tools are already on PATH — that is the caller's job.
# Use direnv, or prefix a single command:  nix develop --command just check

# List the available recipes.
default:
    @just --list

# Convert a USD statement into a sevDesk CSV, filed under run/. Extra flags pass
# straight through:
#   just run wise-usd.csv --since 2026-05-01
run statement *options:
    #!/usr/bin/env bash
    set -uo pipefail
    status=0
    python3 -m sevdesk_importer {{ statement }} {{ options }} || status=$?
    # Exit 2 means the CSV was written and the warnings above are worth reading —
    # a rate substituted from an earlier business day, say. Reporting that as a
    # failed recipe would cry wolf on most runs. Only exit 1, a refusal to write
    # anything at all, is a failure here.
    if [ "$status" -eq 2 ]; then status=0; fi
    exit "$status"

# Everything CI runs.
check: test types fmt-check

# Unit tests. Extra arguments pass through:  just test -k window -v
test *args:
    pytest {{ args }}

# Strict type check.
types:
    mypy

# Reformat the code in place.
fmt:
    ruff format .

# Fail if anything is unformatted, without rewriting it.
fmt-check:
    ruff format --check .

# Build the package and confirm the entry point runs.
build:
    nix build .#sevdesk-importer
    nix run . -- --version
