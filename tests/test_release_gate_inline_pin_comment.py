"""Regression coverage for in-repo testsuite `e2e.yml` pin drift and
inline-comment drift.

The release-gate inline pin comment is repeated across three workflow files:

* ``.github/workflows/reusable-execute-release.yml`` — release-gate
* ``.github/workflows/migration-test.yml``
* ``.github/workflows/upgrade-test.yml``

`docs/skills/composite-actions/reusable-workflow.md` requires:

  * the same SHA in every caller (so Renovate bumps them together)
  * the same ``# v1`` version comment in every caller
  * the inline release-gate comment must not claim a
    ``projectbluefin/bluefin`` ``run-testsuite.yml`` SHA pin that does
    not exist (bluefin uses the floating ``e2e.yml@v1`` managed tag
    there)

The refs must be full 40-char SHAs so the gate executes a fixed digest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_FILES = (
    REPO_ROOT / ".github" / "workflows" / "reusable-execute-release.yml",
    REPO_ROOT / ".github" / "workflows" / "migration-test.yml",
    REPO_ROOT / ".github" / "workflows" / "upgrade-test.yml",
)

E2E_REF_RE = re.compile(
    r"^\s*uses:\s*projectbluefin/testsuite/\.github/workflows/e2e\.yml@(?P<sha>[0-9a-f]{40})"
    r"\s*#\s*(?P<comment>.+?)\s*$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Any wording that asserts the in-repo pin matches a SHA in
# projectbluefin/bluefin's run-testsuite.yml. Bluefin uses the floating
# e2e.yml@v1 managed tag there, so no SHA pin exists to match. (See
# docs/skills/composite-actions/reusable-workflow.md for the verification
# command the comment used to prescribe.)
FORBIDDEN_RELEASE_GATE_PHRASES = (
    "matches projectbluefin/bluefin run-testsuite.yml pin",
    "matches bluefin run-testsuite.yml pin",
    "matches SHA",
    "@v1 resolves to",
)


def _e2e_refs() -> dict[Path, tuple[str, str]]:
    """Return ``{path: (sha, comment)}`` for each in-repo testsuite e2e ref."""
    refs: dict[Path, tuple[str, str]] = {}
    for path in WORKFLOW_FILES:
        matches = []
        for line in path.read_text(encoding="utf-8").splitlines():
            m = E2E_REF_RE.match(line)
            if m:
                matches.append((m.group("sha"), m.group("comment")))
        assert len(matches) == 1, (
            f"{path}: expected exactly one testsuite e2e `uses:` line, "
            f"found {len(matches)}"
        )
        refs[path] = matches[0]
    return refs


def test_repository_pins_testsuite_e2e_in_all_three_callers():
    """Discovery test: the three callers must all pin testsuite e2e.yml."""
    refs = _e2e_refs()
    assert set(refs.keys()) == set(WORKFLOW_FILES)


@pytest.mark.parametrize("workflow", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_caller_uses_a_full_sha(workflow):
    sha, _ = _e2e_refs()[workflow]
    assert SHA_RE.fullmatch(sha), (
        f"{workflow.name}: ref is not a full SHA: {sha!r}"
    )


@pytest.mark.parametrize("workflow", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_caller_uses_the_v1_version_comment(workflow):
    _, comment = _e2e_refs()[workflow]
    assert comment.startswith("v1"), (
        f"{workflow.name}: testsuite e2e ref must use the '# v1' version "
        f"comment so Renovate bumps it together with the other callers; "
        f"got: {comment!r}"
    )


def test_all_callers_share_the_same_sha():
    """Renovate only bumps the SHA for callers on the same `# v1` track."""
    refs = _e2e_refs()
    shas = {sha for sha, _ in refs.values()}
    assert len(shas) == 1, (
        f"testsuite e2e pin drift: callers use different SHAs: "
        f"{ {p.name: sha for p, (sha, _) in refs.items()} }"
    )


def test_release_gate_comment_does_not_claim_a_bluefin_sha_pin():
    """bluefin's run-testsuite.yml pins no SHA — the comment must reflect that."""
    _, comment = _e2e_refs()[WORKFLOW_FILES[0]]
    for phrase in FORBIDDEN_RELEASE_GATE_PHRASES:
        assert phrase not in comment, (
            f"release-gate comment asserts a bluefin SHA pin (or a "
            f"time-bound parenthetical that drifts on every testsuite "
            f"merge): {comment!r} contains {phrase!r}"
        )


def test_release_gate_comment_is_bare_v1():
    """The release-gate comment must be the bare `# v1` Renovate track.

    The other two callers (migration-test.yml, upgrade-test.yml) already
    use plain `# v1`; the release-gate comment must match so all three
    callers are on the same Renovate track.
    """
    _, comment = _e2e_refs()[WORKFLOW_FILES[0]]
    assert comment == "v1", (
        f"release-gate comment must be the bare '# v1' track so Renovate "
        f"bumps it together with the other callers; got: {comment!r}"
    )


@pytest.mark.parametrize(
    "workflow",
    WORKFLOW_FILES[1:],
    ids=lambda p: p.name,
)
def test_migration_and_upgrade_caller_comments_are_bare_v1(workflow):
    """The non-release-gate callers already use bare `# v1`; lock it."""
    _, comment = _e2e_refs()[workflow]
    assert comment == "v1", (
        f"{workflow.name}: testsuite e2e ref must use the bare '# v1' "
        f"track; got: {comment!r}"
    )
