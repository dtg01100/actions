"""Regression coverage for in-repo testsuite `e2e.yml` pin drift.

The release-gate inline pin comment is repeated across three workflow files:

* ``.github/workflows/reusable-execute-release.yml``
* ``.github/workflows/migration-test.yml``
* ``.github/workflows/upgrade-test.yml``

`docs/skills/composite-actions/reusable-workflow.md` requires:

  * the same SHA in every caller (so Renovate bumps them together)
  * the same ``# v1`` version comment in every caller
  * the inline comment must not claim a ``projectbluefin/bluefin
    run-testsuite.yml`` SHA pin that does not exist (bluefin uses the
    floating ``e2e.yml@v1`` managed tag there)

The release-gate comment must also name the testsuite ``@v1`` tag so the
source of truth is unambiguous. The refs must be full 40-char SHAs so the
gate executes a fixed digest.
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
            f"{path}: expected exactly one testsuite e2e `uses:` line, found {len(matches)}"
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
    assert SHA_RE.fullmatch(sha), f"{workflow.name}: ref is not a full SHA: {sha!r}"


@pytest.mark.parametrize("workflow", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_caller_uses_the_v1_version_comment(workflow):
    _, comment = _e2e_refs()[workflow]
    assert comment.startswith("v1"), (
        f"{workflow.name}: testsuite e2e ref must use the '# v1' version "
        f"comment so Renovate bumps it together with the other callers; got: "
        f"{comment!r}"
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
    forbidden = (
        "matches projectbluefin/bluefin run-testsuite.yml pin",
        "matches bluefin run-testsuite.yml pin",
    )
    for phrase in forbidden:
        assert phrase not in comment, (
            f"release-gate comment claims a bluefin SHA pin that does not "
            f"exist: {comment!r} contains {phrase!r}"
        )


def test_release_gate_comment_names_the_v1_tag():
    """The comment must reference the testsuite @v1 tag as the source of truth."""
    _, comment = _e2e_refs()[WORKFLOW_FILES[0]]
    assert "@v1" in comment, (
        f"release-gate comment must reference the testsuite @v1 tag (source "
        f"of truth for the pinned SHA); got: {comment!r}"
    )


def test_release_gate_pin_resolves_to_current_testsuite_v1_tag():
    """The hard-pinned SHA must equal what `testsuite` `refs/tags/v1` resolves to.

    Skipped in environments without network access to github.com; the rest of
    the regression coverage runs locally.
    """
    import subprocess

    refs = _e2e_refs()
    sha, _ = refs[WORKFLOW_FILES[0]]

    try:
        ls_remote = subprocess.run(
            ["git", "ls-remote", "--tags",
             "https://github.com/projectbluefin/testsuite", "refs/tags/v1"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git ls-remote failed in this environment")

    if ls_remote.returncode != 0:
        pytest.skip("git ls-remote could not reach github.com")

    # Format: "<sha>\trefs/tags/v1" (lightweight) or "<sha>\trefs/tags/v1^{}"
    # (annotated; the peeled-commit SHA is on the `^{}` line).
    candidates = []
    for line in ls_remote.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        line_sha, ref = parts
        if ref in ("refs/tags/v1", "refs/tags/v1^{}"):
            candidates.append((ref, line_sha))

    assert candidates, "testsuite v1 tag not advertised via git ls-remote"
    # Annotated-tag case: the `^{}` line has the peeled-commit SHA we want.
    peeled = next((s for r, s in candidates if r == "refs/tags/v1^{}"), None)
    expected = peeled or candidates[0][1]
    assert expected == sha, (
        f"release-gate pin {sha} does not match current testsuite v1 tag "
        f"{expected} — bump the pin per the documented procedure"
    )