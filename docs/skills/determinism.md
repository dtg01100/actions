---
name: determinism
description: Non-deterministic surfaces in the projectbluefin factory — classification, mitigations, and open investigations. Use when auditing build reproducibility, verifying SHA pins are accurate, or investigating why two builds from the same source produce different artifacts.
metadata:
  type: reference
  context7-sources:
    - /websites/github_en_actions
---

# Determinism in the projectbluefin Actions Factory

## Philosophy

Determinism in the factory means: **same inputs → same outputs**. A deterministic build system ensures that rebuilding the same image from the same source code and pinned dependencies produces byte-for-byte identical artifacts. This enables reproducible testing, verifiable releases, and confidence that a known-good build can be replicated.

Non-determinism falls into three categories:
1. **Acceptable drift** — sources outside our control; document and monitor
2. **Already pinned** — surface is locked; verify and maintain
3. **Under investigation** — requires fixes or trade-off decisions

---

## Gold Standards (Model These Patterns)

### 1. SHA-locking in weekly testing promotion (`bluefin/.github/workflows/weekly-testing-promotion.yml`)

**Pattern:** Before promoting a testing build to stable, lock the main branch HEAD SHA:

```bash
SHA=$(gh api repos/${{ github.repository }}/git/ref/heads/main --jq '.object.sha')
```

Then verify that e2e tests passed **on that exact SHA** before proceeding. This ensures:
- No hidden commits between decision point and build
- Reproducible promotion logic
- Auditable trail (SHA is part of workflow state)

**Why it works:** Git SHAs are immutable content hashes. Once locked, the repository state is deterministic.

### 2. E2E gate locks to source_branch HEAD SHA — not a hardcoded ref

**Pattern:** The promote-squash E2E gate queries the *branch* that E2E workflows run against
(e.g. `testing`), resolves its current HEAD SHA at gate-time, and verifies that E2E passed
**on that exact SHA**:

```bash
SHA=$(gh api repos/$REPO/git/ref/heads/$E2E_HEAD_BRANCH --jq '.object.sha')
```

Never use a hardcoded branch name (`main`) or the caller's `github.ref` as the gate target.
Either can match a commit that hasn't had E2E run yet, allowing untested code through.

**Why it matters:** Git branch refs are mutable. The same branch name resolves to a different
commit between the E2E run and the gate check if any push lands in between. Locking to the
resolved SHA at gate-time closes this race.

---

### 3. Pinned build engine in dakota (`dakota/.github/actions/check-bst2-pin/`)

**Pattern:** BuildStream 2 (the compilation engine) is pinned to an exact container image SHA in both the Justfile and CI workflow:

```bash
just_sha="$(grep -oE 'bst2:[a-f0-9]{40}' Justfile)"
track_sha="$(grep -oE 'bst2:[a-f0-9]{40}' .github/workflows/track-bst-sources.yml)"
```

A CI check enforces they match. This ensures:
- Compiler version cannot drift between local and CI builds
- BST2 updates are deliberate and synchronized
- Reproducible builds across all developers and CI runners

**Why it works:** Container image SHAs are immutable. Pinning the builder guarantees identical compilation behavior.

---

## Acceptable Drift (Document, Do Not Eliminate)

### 1. Upstream RPM versions

**Surface:** Fedora updates base packages daily. Builds on different dates pull different RPM versions.

**Why acceptable:**
- Pinning every RPM creates maintenance burden
- Security updates are critical and frequent
- `dnf cache` action mitigates by reusing cached layers
- Latest-stable is the right policy for desktop OS (Bluefin)

**Mitigation:**
- DNF cache in reusable workflow reduces re-download of unchanged packages
- Document expected drift in release notes
- Run weekly e2e testing to catch breakage early

**Status:** ✅ Intentional, monitored via weekly CI

### 2. Cron schedule timing and runner OS freshness

**Surface:** GitHub Actions runners are updated weekly; `cron: '0 6 * * 2'` may run on different patch levels of Ubuntu 24.04.

**Why acceptable:**
- Runner OS updates are security-critical
- Patch-level differences in Ubuntu are minimal (same kernel ABI)
- Workflow runs at consistent UTC time, not wall-clock time

**Mitigation:**
- Document runner image version in build logs
- Use stable runner labels (`ubuntu-24.04`, `ubuntu-24.04-arm`)
- Monitor for runner-specific failures in e2e tests

**Status:** ✅ Acceptable, runner selection is deliberate

### 3. Container storage initialization order

**Surface:** BTRFS loopback mount (`setup-runner` input `storage-backend: btrfs`) initializes before each build. Mount options and loopback device numbering vary between runs.

**Why acceptable:**
- BTRFS initialization is stable and repeatable on the same runner
- The compressed filesystem is transparent to the build
- Layer ordering inside a rechunked image is pinned by the chunkah build clock
  (see "Chunkah build clock" below) — storage layout itself does not affect it

**Mitigation:**
- Use `compress-force=zstd:2` to ensure consistent compression
- Verify in smoke tests that layer digests match between builds
- Monitor for storage-related flakes in CI

**Status:** ✅ Acceptable, reproducible within same runner pool

---

## Already Pinned (Verify and Maintain)

### 1. Third-party action SHAs in all composite actions

**Pattern:** Every `uses:` reference in `bootc-build/*/action.yml` and `.github/workflows/reusable-build.yml` must be pinned to a full commit SHA with a version comment. No floating tags (`@main`, `@v3`, `@latest`).

**How to verify the repo is clean:**
```bash
# Find any floating tags — should return nothing
grep -r 'uses:' bootc-build/ .github/workflows/ \
  | grep -v '@[0-9a-f]\{40\}'
```

**How to verify a SHA comment is accurate (not a pre-release branch tip):**
```bash
# Check that the comment tag exists as a real release, not just a branch ref
gh release view v6.0.3 --repo actions/checkout --json tagName
# If no release exists, use: # no-release, <landmark> (YYYY-MM)
```

**Version comment rules:**
- Use the **exact** release tag: `# v6.0.3` not `# v6`
- For repos with no releases/tags: `# no-release, Merge PR #N (YYYY-MM)`
- Renovate bumps SHAs in consuming repos — the canonical pins live here

**Chunkah container SHA:**
- Version and digest both pinned in `chunka/action.yml` (`CHUNKAH_VERSION` + `CHUNKAH_SHA`)
- Bump both together when upgrading — they derive the image ref and the Containerfile.splitter URL

**Dakota BST2 pin:**
- Enforced by `dakota/.github/actions/check-bst2-pin/` consistency check
- Pinned in both Justfile and workflow — CI blocks drift

### 2. Chunkah build clock (`--source-date-epoch`)

**Pattern:** `bootc-build/chunka` takes a `source-date-epoch` input and forwards it to chunkah as
`--source-date-epoch`. Its default is `auto`, which resolves the committer timestamp of the
checked-out source (`git log -1 --format=%ct HEAD` in `$GITHUB_WORKSPACE`), so every caller —
including `reusable-build.yml` — gets a pinned clock without wiring anything.

**Why it matters — the epoch is not cosmetic.** chunkah resolves a single "now" (explicit
`--source-date-epoch`, else `SOURCE_DATE_EPOCH`, else the `Created` field of the config it is
given, else the wall clock) and uses it for three separate things:

1. the image `created` timestamp,
2. the default mtime clamp, applied as `min(file mtime, epoch)` to every file whose component
   has no reproducible clamp (xattr-claimed files, unclaimed files, big files) — RPM components
   use the rpmdb build time and are unaffected,
3. the `now` argument to `calculate_stability()` for every component.

(3) is the one that bites. Stability drives the tier thresholds (mean ± stddev), the packing
bins, and the final `sort_by_stability_desc`. On a wall clock, two builds of identical content
minutes apart can score the same components differently, re-bin them, and emit **the same layer
blobs in a different order**. Content-addressed blobs mean nobody re-downloads anything, but the
manifest changes, so the image digest changes for a build that changed nothing.

**Rules:**
- Never let chunkah reach the wall clock in a build that is expected to be reproducible. The
  fallback is the source image's `Created` field, which for a freshly built base image is itself
  a build timestamp — no better than the wall clock.
- The epoch is a clamp, not a stamp: it may safely be older than the files being written.
- Accept `auto`, digits, or empty. Anything else fails the action before chunkah runs, because the
  buildah path splices the value into the `CHUNKAH_ARGS` build arg that the vendored
  `Containerfile.splitter` interpolates into a shell line.
- `auto` outside a git checkout warns and builds unpinned rather than failing the build. Treat
  that warning as a broken checkout, not as a normal outcome.
- Consumers whose image is *not* built from the checked-out tree (a pinned base, a rebased
  Containerfile) should pass an explicit timestamp instead of relying on `auto`.

**How to verify:**
```bash
# Same commit, two builds: the digests and the layer lists must match.
skopeo inspect --raw "docker://<registry>/<image>@<digest-a>" | jq -S '.layers[].digest'
skopeo inspect --raw "docker://<registry>/<image>@<digest-b>" | jq -S '.layers[].digest'
```
If the sorted digest lists match but the order differs, the build clock is not pinned — look for
`chunkah pinned to SOURCE_DATE_EPOCH=` in the chunka step log, or for the `auto` warning.

---

## Open Investigations

### 1. Chunkah reproducibility beyond the build clock

**Question:** With `--source-date-epoch` pinned, is a rechunked image byte-identical across
builds of the same commit?

**Already closed:** the build clock. The wall clock was the *only* `now` chunkah used, and it fed
the stability scores that decide packing and layer order. See "Chunkah build clock" above.

**Still open:** the source image reaching chunkah must itself be reproducible. Differences in the
base image — package versions picked up by a rebuild, tar header mtimes written by the build —
change layer content, which no amount of epoch pinning can undo. Two builds of the same commit on
different days legitimately produce different digests when package versions moved.

**How to check:** build the same commit twice within a short window (so package versions match),
compare manifests, and classify every difference: same blobs in a different order is a build-clock
regression, different blobs are a content difference.

**Status:** 🟡 Build clock pinned; source-image reproducibility tracked in the consumer repos

### 2. SOURCE_DATE_EPOCH in Containerfile builds

**Question:** Are Containerfile builds using SOURCE_DATE_EPOCH to pin timestamps?

**Analysis:**
- The chunkah side is pinned: `bootc-build/chunka` defaults `source-date-epoch` to `auto` and
  forwards it as `--source-date-epoch` (see "Chunkah build clock")
- Neither bluefin nor dakota sets `SOURCE_DATE_EPOCH` in Containerfiles or Justfiles for the
  *base image* build, so the layers chunkah later repacks can still carry build-time mtimes
- Podman/buildah respect the variable when it is set in the runner environment

**Impact:**
- Minimal for bootc images (timestamps in /etc are not part of runtime state)
- Relevant for SBOM metadata (generation timestamp is recorded)
- Could affect reproducible builds if using timestamps for verification

**Current mitigation:**
- SBOM generation captures workflow run timestamp separately
- Attestations include build metadata (run ID, timestamp)

**Next steps:**
1. Verify that OSTree commit hashes are deterministic (not timestamp-dependent)
2. For cross-repo reproducible base images, set `SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)` in
   the consumer's base-image build (Justfile/Containerfile), not only in the rechunk step

**Status:** 🟡 Rechunk step pinned; base-image build still on the wall clock

### 3. AT-SPI test flakes in e2e testing

**Question:** Why do Accessibility (AT-SPI) tests occasionally fail in e2e runs?

**Context:**
- post-testing-e2e runs smoke tests before weekly promotion
- AT-SPI dbus initialization can be timing-sensitive
- Runs on shared GitHub Actions runners with variable load

**Current mitigation:**
- Rerun button available if flake occurs
- Monitoring in project board for test stability

**Next steps:**
- Add AT-SPI service availability check to e2e harness
- Consider longer timeout for accessibility tests
- Log detailed dbus trace on failure

**Status:** 🟡 Known intermittent; needs CI harness improvement

---

## Audit Schedule and Ownership

| Item | Cadence | Owner |
|------|---------|-------|
| Third-party action SHA updates | Monthly (via Renovate PR review) | @castrojo |
| Chunkah reproducibility test | Quarterly | Agent-run (reproducibility CI) |
| Chunkah build clock pin | Per chunkah bump | Agent-run (unit tests guard the wiring) |
| SOURCE_DATE_EPOCH decision | Next design review | Architecture review |
| AT-SPI test flake analysis | As-needed (PR blocking) | Whoever hits it in CI |

---

## How to Use This File

- **Floating tag found in a new workflow?** → Flag under "Critical: must fix" and add to `actionlint` check
- **Need to update a pinned action?** → Search this file for the action name, verify new SHA, update with version comment
- **New non-deterministic surface discovered?** → Add to the appropriate section and create a tracking issue
- **Investigation resolved?** → Move section to "Already pinned" with mitigation summary

---

## When to Use

Use this skill when comparing artifacts from repeated builds, reviewing dependency or runner
pinning, or deciding whether observed output drift is acceptable.

## When NOT to Use

Do not use this skill to debug a single workflow failure with no reproducibility signal, or to
change a consumer's release policy without an explicit design decision.

## Core Process

1. Identify the input, output, and exact build boundary that differs.
2. Classify the surface as acceptable drift, already pinned, or under investigation.
3. Verify immutable references and compare logs, metadata, and digests at that boundary.
4. Record mitigation and an owner; create follow-up work when drift is unresolved.

## Common Rationalizations

- "The tag is stable enough." Tags and branch refs remain mutable; resolve and compare SHAs.
- "Only one architecture matters." Multi-architecture builds can diverge independently.
- "The digest changed, so the source changed." First rule out timestamps, runner state, and
  platform-specific image digests.

## Red Flags

- Floating action, image, branch, or dependency references in a reproducibility-critical path.
- A changed artifact without a recorded input SHA or build metadata.
- A mitigation that relies on rerunning until output happens to match.

## Verification

- [ ] Input commit, dependency pins, architecture, and runner are recorded.
- [ ] Output digests or hashes are compared at the same artifact boundary.
- [ ] Drift is classified and mitigation is documented.
- [ ] Unresolved drift has a tracking issue and owner.

---

## Related Reading

- [`docs/skills/composite-actions.md`](composite-actions.md) — SHA pinning conventions and adding new actions
- [`docs/skills/consumer-guide.md`](consumer-guide.md) — How consuming repos stay in sync with factory updates
- `bluefin/.github/workflows/weekly-testing-promotion.yml` — Gold standard for SHA-locking
- `dakota/.github/actions/check-bst2-pin/` — Gold standard for builder pinning
