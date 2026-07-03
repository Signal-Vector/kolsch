"""``kolsch skills install <url>`` — Step 7 URL → bundle install.

Uses a fake fetcher (the production code's documented test seam) so
the suite has no network dependency. The real ``_real_fetcher``
exercises ``git clone`` and ``urllib.request.urlretrieve``; those
are covered by the manual e2e in the PR description.

Cases covered:

- Auto-create flow: bundle.yaml is well-formed, skill lands at the
  derived ref, post-install validate fires.
- ``--bundle <existing>`` installs into that bundle.
- ``--bundle <missing>`` fails BEFORE fetch (the fake fetcher's
  call counter proves it wasn't invoked).
- Strict-root SKILL.md absent → friendly error + zero workspace
  writes (atomicity contract).
- ``--yes`` bypasses the trust prompt (pytest's non-TTY stdin
  would otherwise refuse).
- Non-TTY caller without ``--yes`` refuses cleanly.
- Existing skill dir → refuses to overwrite.
- Bundle-name collision → numeric discriminator (``-2``, ``-3``).
"""

from __future__ import annotations

import io as _io
import shutil
from typing import TYPE_CHECKING

import pytest
import yaml

from cli.skills import _cmd_install, _ParsedInstallUrl

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Workspace + fixture helpers.
# ---------------------------------------------------------------------------


def _platform_yaml(root: Path) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/", "bundles/"],
                    "denied_paths": [".env"],
                },
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")


def _make_bundle(root: Path, name: str) -> Path:
    bundle = root / "bundles" / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1", "description": "x"}),
        encoding="utf-8",
    )
    return bundle


def _build_skill_source(
    tmp_path: Path,
    *,
    include_skill_md: bool = True,
    include_scripts: bool = False,
) -> Path:
    """Create a directory shaped like a fetched skill checkout.

    The fake fetcher copies this verbatim into the install pipeline's
    scratch dir, so this is the exact view the trust prompt + the
    strict-root check + the atomic move all operate on.
    """
    source = tmp_path / "fake_remote"
    source.mkdir(parents=True, exist_ok=True)
    if include_skill_md:
        (source / "SKILL.md").write_text(
            "---\n"
            "name: test-writer\n"
            "description: Write tests in the project's idiom.\n"
            "---\n\n"
            "# test-writer\n\n"
            "Use this skill when authoring tests.\n",
            encoding="utf-8",
        )
    if include_scripts:
        (source / "scripts").mkdir()
        (source / "scripts" / "extract.py").write_text(
            "# stub helper\n", encoding="utf-8"
        )
    return source


def _fake_fetcher(source: Path, *, calls: list[int] | None = None):
    """Build a fake fetcher that copies ``source`` into the install
    pipeline's scratch dir. ``calls`` (optional) is a counter list
    the test can inspect to assert the fetcher was (or wasn't)
    invoked — used by the fast-fail tests."""

    def _f(parsed: _ParsedInstallUrl, scratch: Path) -> Path:
        if calls is not None:
            calls.append(1)
        del parsed  # unused
        dest = scratch / "checkout"
        shutil.copytree(source, dest)
        return dest

    return _f


_FAKE_URL = "https://github.com/owner/test-writer"


# ---------------------------------------------------------------------------
# Happy path — auto-create bundle.
# ---------------------------------------------------------------------------


def test_install_auto_creates_bundle_with_well_formed_manifest(
    tmp_path: Path,
) -> None:
    """No ``--bundle`` flag: install auto-creates a bundle named
    after the URL's last meaningful segment, writes a well-formed
    bundle.yaml, and lands the skill at <bundle>/skills/<name>."""
    _platform_yaml(tmp_path)
    source = _build_skill_source(tmp_path, include_scripts=True)
    out = _io.StringIO()
    err = _io.StringIO()

    exit_code = _cmd_install(
        tmp_path,
        url=_FAKE_URL,
        bundle=None,
        bundle_name=None,
        yes=True,
        out=out,
        err=err,
        fetcher=_fake_fetcher(source),
    )
    assert exit_code == 0, f"stdout: {out.getvalue()}; stderr: {err.getvalue()}"

    new_bundle = tmp_path / "bundles" / "test-writer"
    assert (new_bundle / "bundle.yaml").is_file()
    bundle_payload = yaml.safe_load((new_bundle / "bundle.yaml").read_text())
    assert bundle_payload["name"] == "test-writer"
    assert "version" in bundle_payload and "description" in bundle_payload

    skill_md = new_bundle / "skills" / "test-writer" / "SKILL.md"
    assert skill_md.is_file()
    assert (new_bundle / "skills" / "test-writer" / "scripts" / "extract.py").is_file()

    rendered = out.getvalue()
    assert "installed test-writer/skills/test-writer" in rendered
    # Post-install validate fires (the validate output uses [OK]).
    assert "[OK]" in rendered


def test_install_with_bundle_name_uses_explicit_name(tmp_path: Path) -> None:
    """``--bundle-name <name>`` overrides the URL-derived bundle
    name (still auto-creates; ``--bundle`` would be the "install
    into existing" form)."""
    _platform_yaml(tmp_path)
    source = _build_skill_source(tmp_path)

    exit_code = _cmd_install(
        tmp_path,
        url=_FAKE_URL,
        bundle=None,
        bundle_name="my-skills",
        yes=True,
        out=_io.StringIO(),
        err=_io.StringIO(),
        fetcher=_fake_fetcher(source),
    )
    assert exit_code == 0
    assert (tmp_path / "bundles" / "my-skills" / "bundle.yaml").is_file()
    assert (
        tmp_path / "bundles" / "my-skills" / "skills" / "test-writer" / "SKILL.md"
    ).is_file()


# ---------------------------------------------------------------------------
# --bundle into an existing bundle.
# ---------------------------------------------------------------------------


def test_install_into_existing_bundle(tmp_path: Path) -> None:
    """``--bundle <existing>`` installs at
    bundles/<bundle>/skills/<skill_name>/ without creating a new
    bundle.yaml."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    source = _build_skill_source(tmp_path)

    exit_code = _cmd_install(
        tmp_path,
        url=_FAKE_URL,
        bundle="acme",
        bundle_name=None,
        yes=True,
        out=_io.StringIO(),
        err=_io.StringIO(),
        fetcher=_fake_fetcher(source),
    )
    assert exit_code == 0

    skill_md = (
        tmp_path / "bundles" / "acme" / "skills" / "test-writer" / "SKILL.md"
    )
    assert skill_md.is_file()
    # The original acme/skills/bundle.yaml is unchanged — no auto-create
    # touched it.
    payload = yaml.safe_load((tmp_path / "bundles" / "acme" / "bundle.yaml").read_text())
    assert payload["name"] == "acme"


# ---------------------------------------------------------------------------
# Fast-fail paths — destination check BEFORE fetch.
# ---------------------------------------------------------------------------


def test_install_unknown_bundle_fails_before_fetch(tmp_path: Path) -> None:
    """``--bundle <nonexistent>`` exits 1 with a friendly error
    BEFORE invoking the fetcher (verified via call counter). Saves
    the user a clone they'd throw away on a typo."""
    _platform_yaml(tmp_path)
    source = _build_skill_source(tmp_path)
    calls: list[int] = []
    err = _io.StringIO()

    exit_code = _cmd_install(
        tmp_path,
        url=_FAKE_URL,
        bundle="does-not-exist",
        bundle_name=None,
        yes=True,
        out=_io.StringIO(),
        err=err,
        fetcher=_fake_fetcher(source, calls=calls),
    )
    assert exit_code == 1
    assert calls == [], "fetcher must NOT have been invoked"
    assert "bundle 'does-not-exist' not found" in err.getvalue()


def test_install_strict_root_failure_no_partial_write(tmp_path: Path) -> None:
    """When the fetched directory has no SKILL.md at its root, the
    install fails cleanly and the workspace's bundles/ tree gains
    nothing — the atomicity contract."""
    _platform_yaml(tmp_path)
    source = _build_skill_source(tmp_path, include_skill_md=False)
    err = _io.StringIO()
    out = _io.StringIO()

    exit_code = _cmd_install(
        tmp_path,
        url=_FAKE_URL,
        bundle=None,
        bundle_name=None,
        yes=True,
        out=out,
        err=err,
        fetcher=_fake_fetcher(source),
    )
    assert exit_code == 1
    assert "does not point at a SKILL.md root" in err.getvalue()

    # Nothing landed on disk — the auto-create bundle dir never
    # gets staged when validation fails (rollback-by-construction:
    # writes only happen INSIDE the scratch dir until move time).
    bundles_dir = tmp_path / "bundles"
    # The platform-yaml fixture created an empty bundles/ already;
    # what matters is that no test-writer/ landed.
    assert not (bundles_dir / "test-writer").exists()


def test_install_refuses_overwrite_existing_skill(tmp_path: Path) -> None:
    """``--bundle <existing>`` + a skill dir already at the target
    path → refuse to overwrite. Mirrors the ``skills new`` guard."""
    _platform_yaml(tmp_path)
    bundle = _make_bundle(tmp_path, "acme")
    pre_existing = bundle / "skills" / "test-writer"
    pre_existing.mkdir(parents=True)
    (pre_existing / "SKILL.md").write_text(
        "---\nname: test-writer\ndescription: pre-existing\n---\n\nbody\n",
        encoding="utf-8",
    )
    source = _build_skill_source(tmp_path)
    err = _io.StringIO()

    exit_code = _cmd_install(
        tmp_path,
        url=_FAKE_URL,
        bundle="acme",
        bundle_name=None,
        yes=True,
        out=_io.StringIO(),
        err=err,
        fetcher=_fake_fetcher(source),
    )
    assert exit_code == 1
    assert "already exists" in err.getvalue()


# ---------------------------------------------------------------------------
# Trust prompt + --yes.
# ---------------------------------------------------------------------------


def test_install_non_tty_without_yes_refuses(tmp_path: Path) -> None:
    """A non-TTY caller (CI, scripts, pytest) without ``--yes`` is
    refused — silent stdin would otherwise hang or, worse, install
    blindly on a misread. The error names ``--yes`` as the remedy."""
    _platform_yaml(tmp_path)
    source = _build_skill_source(tmp_path)
    err = _io.StringIO()

    exit_code = _cmd_install(
        tmp_path,
        url=_FAKE_URL,
        bundle=None,
        bundle_name=None,
        yes=False,
        out=_io.StringIO(),
        err=err,
        fetcher=_fake_fetcher(source),
    )
    assert exit_code == 1
    rendered_err = err.getvalue()
    assert "TTY" in rendered_err
    assert "--yes" in rendered_err
    # Nothing landed.
    assert not (tmp_path / "bundles" / "test-writer").exists()


def test_install_yes_prints_trust_audit_lines(tmp_path: Path) -> None:
    """``--yes`` bypasses the prompt but still prints the trust
    surface (URL, destination, file list, allowed-tools). The audit
    trail stays honest even when the user opted into auto-accept."""
    _platform_yaml(tmp_path)
    source = _build_skill_source(tmp_path, include_scripts=True)
    out = _io.StringIO()

    exit_code = _cmd_install(
        tmp_path,
        url=_FAKE_URL,
        bundle=None,
        bundle_name=None,
        yes=True,
        out=out,
        err=_io.StringIO(),
        fetcher=_fake_fetcher(source),
    )
    assert exit_code == 0

    rendered = out.getvalue()
    assert f"Install from: {_FAKE_URL}" in rendered or _FAKE_URL in rendered
    assert "SKILL.md" in rendered
    assert "scripts/extract.py" in rendered
    assert "allowed-tools" in rendered.lower()
    assert "Proceeding (--yes)" in rendered


# ---------------------------------------------------------------------------
# Collision discriminator on auto-create.
# ---------------------------------------------------------------------------


def test_install_auto_create_appends_discriminator_on_collision(
    tmp_path: Path,
) -> None:
    """Two installs of the same URL → the second auto-create lands
    in ``test-writer-2`` because ``test-writer/`` already exists."""
    _platform_yaml(tmp_path)
    source = _build_skill_source(tmp_path)

    for _ in range(2):
        exit_code = _cmd_install(
            tmp_path,
            url=_FAKE_URL,
            bundle=None,
            bundle_name=None,
            yes=True,
            out=_io.StringIO(),
            err=_io.StringIO(),
            fetcher=_fake_fetcher(source),
        )
        assert exit_code == 0

    assert (tmp_path / "bundles" / "test-writer" / "bundle.yaml").is_file()
    assert (tmp_path / "bundles" / "test-writer-2" / "bundle.yaml").is_file()


# ---------------------------------------------------------------------------
# URL parser — covering the GitHub sub-path translation.
# ---------------------------------------------------------------------------


def test_parse_install_url_github_subpath_extracts_branch_and_subpath() -> None:
    """GitHub ``/tree/<branch>/<subpath>`` form translates to a git
    clone of the bare repo URL + branch + subpath. The derived name
    is the last segment of the subpath, not the repo name."""
    from cli.skills import _parse_install_url

    parsed = _parse_install_url(
        "https://github.com/anthropic/agent-skills/tree/main/skills/test-writer",
    )
    assert parsed.kind == "git"
    assert parsed.fetch_url == "https://github.com/anthropic/agent-skills.git"
    assert parsed.branch == "main"
    assert parsed.subpath == "skills/test-writer"
    assert parsed.derived_name == "test-writer"


def test_parse_install_url_github_repo_root_uses_repo_name() -> None:
    """Repo-root GitHub URL: no branch, no subpath, derived name is
    the repo's name."""
    from cli.skills import _parse_install_url

    parsed = _parse_install_url("https://github.com/owner/cool-skill")
    assert parsed.kind == "git"
    assert parsed.fetch_url == "https://github.com/owner/cool-skill.git"
    assert parsed.branch is None
    assert parsed.subpath is None
    assert parsed.derived_name == "cool-skill"


def test_parse_install_url_archive_recognised_by_extension() -> None:
    """``.tar.gz`` / ``.zip`` URLs route through the archive
    fetcher, not git clone."""
    from cli.skills import _parse_install_url

    parsed = _parse_install_url(
        "https://example.com/releases/test-writer-1.2.0.tar.gz",
    )
    assert parsed.kind == "archive"
    assert parsed.derived_name == "test-writer-1-2-0"


def test_parse_install_url_unsupported_scheme_raises(
    tmp_path: Path,
) -> None:
    """An unsupported scheme (file://, ftp://, ssh://) raises with
    an actionable message naming https://."""
    from cli.skills import _parse_install_url

    del tmp_path
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        _parse_install_url("file:///tmp/local/skill")
