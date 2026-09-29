#!/usr/bin/env bats
# Tests for the chunkah determinism plumbing (#591).
#
# chunkah's "now" — its --source-date-epoch / SOURCE_DATE_EPOCH — is not cosmetic:
# it is the mtime clamp used for files with no known build time, and the `now`
# every component stability score is computed against. Stability decides the
# stability tiers, the packing bins and the final layer sort, so on a wall clock
# two builds of identical content can emit the same layer blobs in a different
# order: new manifest, new image digest, nothing actually changed.
#
# These tests extract the real block from bootc-build/chunka/action.yml, so they
# exercise shipped code rather than a copy. No container runtime, no privileges.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
CHUNKA_ACTION="${REPO_ROOT}/bootc-build/chunka/action.yml"

setup() {
  TEST_TMP=$(mktemp -d)
  export TEST_TMP
}

teardown() {
  rm -rf "$TEST_TMP"
}

# Print the run block of the chunka action.
chunka_run_block() {
  python3 - "$CHUNKA_ACTION" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
print(doc["runs"]["steps"][0]["run"], end="")
PY
}

# Print the SDE_ARGS preamble of the chunka run block, dedented, ready to source.
chunka_sde_preamble() {
  chunka_run_block | awk '
    /^# Deterministic build clock/ { grab = 1 }
    grab && /^# CHUNKAH_CONFIG_STR/ { exit }
    grab { print }
  '
}

# Print the CHUNKAH_ARGS assignment of the chunka run block, dedented.
chunka_args_assignment() {
  chunka_run_block | awk '
    /^ *# SDE_ARGS is spliced/ { grab = 1; next }
    grab && /^ *CHUNKAH_ARGS=/ { print; exit }
  '
}

# Run the extracted preamble with the given source-date-epoch input value.
run_preamble() {
  bash -c '
    set -euo pipefail
    MAX_LAYERS="128"
    SOURCE_DATE_EPOCH="$1"
    GITHUB_WORKSPACE="$2"
    '"$(chunka_sde_preamble)"'
    '"$(chunka_args_assignment)"'
    echo "CHUNKAH_ARGS=${CHUNKAH_ARGS}"
  ' _ "$1" "${2:-}"
}

# A git repo with one commit whose committer date is pinned to $EPOCH.
make_repo() {
  local repo="$1" epoch="$2"
  mkdir -p "$repo"
  git -C "$repo" init -q
  git -C "$repo" config user.email hive@example.com
  git -C "$repo" config user.name hive
  GIT_AUTHOR_DATE="@${epoch}" GIT_COMMITTER_DATE="@${epoch}" \
    git -C "$repo" commit -q --allow-empty -m "build me"
}

# ── argument construction ────────────────────────────────────────────────────

@test "chunka: an empty input leaves chunkah on its own default" {
  run run_preamble ""
  [ "$status" -eq 0 ]
  [[ "$output" == *"CHUNKAH_ARGS="* ]]
  [[ "$output" != *"--source-date-epoch"* ]]
}

@test "chunka: a numeric epoch becomes a --source-date-epoch argument" {
  run run_preamble "1758000000"
  [ "$status" -eq 0 ]
  [[ "$output" == *"chunkah pinned to SOURCE_DATE_EPOCH=1758000000"* ]]
  [[ "$output" == *"--source-date-epoch 1758000000"* ]]
}

@test "chunka: a non-numeric epoch is rejected before chunkah runs" {
  run run_preamble "2026-09-27"
  [ "$status" -ne 0 ]
  [[ "$output" == *"::error::source-date-epoch must be"* ]]
  [[ "$output" != *"CHUNKAH_ARGS="* ]]
}

@test "chunka: an argument-injection attempt in the epoch is rejected" {
  run run_preamble "1 --label evil"
  [ "$status" -ne 0 ]
  [[ "$output" == *"::error::source-date-epoch must be"* ]]
  # The value is echoed in the error message, so assert on control flow instead:
  # nothing after the validation ran, so no argument array was ever built.
  [[ "$output" != *"CHUNKAH_ARGS="* ]]
}

# ── auto resolution ──────────────────────────────────────────────────────────

@test "chunka: auto resolves the committer timestamp of the checked-out HEAD" {
  local repo="${TEST_TMP}/src"
  make_repo "$repo" 1758000000
  [ "$(git -C "$repo" log -1 --format=%ct)" = "1758000000" ]

  run run_preamble "auto" "$repo"
  [ "$status" -eq 0 ]
  [[ "$output" == *"auto resolved to 1758000000"* ]]
  [[ "$output" == *"--source-date-epoch 1758000000"* ]]
}

@test "chunka: auto is the default input value" {
  python3 - "$CHUNKA_ACTION" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
inp = doc["inputs"]["source-date-epoch"]
assert inp.get("default") == "auto", inp.get("default")
env = doc["runs"]["steps"][0]["env"]
assert env["SOURCE_DATE_EPOCH"] == "${{ inputs.source-date-epoch }}", env["SOURCE_DATE_EPOCH"]
PY
}

@test "chunka: auto outside a checkout warns and builds unpinned instead of failing" {
  mkdir -p "${TEST_TMP}/no-repo"
  run run_preamble "auto" "${TEST_TMP}/no-repo"
  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::"* ]]
  [[ "$output" == *"will not be reproducible"* ]]
  [[ "$output" != *"--source-date-epoch"* ]]
}

# ── CHUNKAH_ARGS construction (the buildah path) ─────────────────────────────

@test "chunka: CHUNKAH_ARGS carries the epoch through the buildah path" {
  run run_preamble "1758000000"
  [ "$status" -eq 0 ]
  [[ "$output" == *"--max-layers 128"* ]]
  [[ "$output" == *"--source-date-epoch 1758000000"* ]]
}

@test "chunka: CHUNKAH_ARGS is unchanged when no epoch is supplied" {
  run run_preamble ""
  [ "$status" -eq 0 ]
  [[ "$output" == *"--label ostree.final-diffid- "* ]]
  [[ "$output" != *"--source-date-epoch"* ]]
}

# ── wiring ───────────────────────────────────────────────────────────────────

@test "chunka: both the BST and the buildah path forward the epoch" {
  # BST path: array expansion on the chunkah invocation.
  grep -qF '"${SDE_ARGS[@]}" \' "$CHUNKA_ACTION"
  # buildah path: spliced into the CHUNKAH_ARGS build arg.
  grep -qF -- '--build-arg "CHUNKAH_ARGS=${CHUNKAH_ARGS}" \' "$CHUNKA_ACTION"
}
