"""Teams app package — manifest validation + zipper script smoke.

Slice 3.1.e ships ``appPackage/manifest.json`` + ``scripts/teams-package.sh``.
These tests guard the package's basic shape so a typo doesn't ship
a manifest Teams Admin Center will reject without telling us why.

What we check
-------------

* **Manifest JSON shape** — required Teams v1.16 fields exist, types
  match, the bots[0].botId / id sentinel is the documented placeholder,
  scopes are spelled correctly, commandLists cover personal + team.
* **Zipper script** — invoking ``scripts/teams-package.sh`` produces a
  valid zip with manifest.json + icons/ at the root, env var
  substitutions land in the output manifest, and the placeholder
  warning fires when the env vars are unset.

Out of scope here: real Teams Admin Center validation (that's a manual
sideload step in the runbook) and the Bicep build/lint (CI's existing
``bicep`` job covers that).
"""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

# Paths resolved from the test file so the suite works regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_APP_PACKAGE_DIR = _REPO_ROOT / "appPackage"
_ZIPPER_SCRIPT = _REPO_ROOT / "scripts" / "teams-package.sh"
_MANIFEST_PATH = _APP_PACKAGE_DIR / "manifest.json"

_PLACEHOLDER_APP_ID = "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# manifest.json shape
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> dict:
    """Parse the committed manifest once per test module."""
    return json.loads(_MANIFEST_PATH.read_text())


@pytest.mark.unit
def test_manifest_uses_teams_v1_16_schema(manifest: dict) -> None:
    """Schema URL anchors what fields the manifest can use. Bumping
    requires verifying every dependent field still validates."""
    assert manifest["$schema"].endswith("v1.16/MicrosoftTeams.schema.json")
    assert manifest["manifestVersion"] == "1.16"


@pytest.mark.unit
def test_manifest_has_required_top_level_fields(manifest: dict) -> None:
    """Teams Admin Center hard-rejects manifests missing any of these.
    Catch the omissions here so a bad commit doesn't surface at upload."""
    required = {
        "id",
        "packageName",
        "version",
        "developer",
        "name",
        "description",
        "icons",
        "accentColor",
        "bots",
    }
    missing = required - manifest.keys()
    assert not missing, f"missing required fields: {missing}"


@pytest.mark.unit
def test_manifest_id_is_placeholder_sentinel(manifest: dict) -> None:
    """The committed manifest carries a sentinel UUID; the zipper script
    substitutes it from ``MOVATE_TEAMS_BOT_APP_ID`` at build time. Any
    drift from this sentinel means someone committed a real id, which
    leaks the bot's identity into the public repo."""
    assert manifest["id"] == _PLACEHOLDER_APP_ID
    assert manifest["bots"][0]["botId"] == _PLACEHOLDER_APP_ID


@pytest.mark.unit
def test_manifest_bot_scopes_cover_personal_team_groupchat(manifest: dict) -> None:
    """Identity commands are DM-only via the handler; everything else
    works in channels + group chats. All three scopes must be present."""
    scopes = manifest["bots"][0]["scopes"]
    assert set(scopes) == {"personal", "team", "groupchat"}


@pytest.mark.unit
def test_manifest_bot_supports_files(manifest: dict) -> None:
    """Slice 3.1.d depends on file uploads — the manifest must declare
    file support so Teams exposes the paperclip in the compose box."""
    assert manifest["bots"][0]["supportsFiles"] is True


@pytest.mark.unit
def test_manifest_command_lists_cover_global_and_dm_commands(manifest: dict) -> None:
    """The compose-box autocomplete surfaces commands per scope.
    Identity commands belong to the personal-only list because they
    don't make sense in a channel."""
    command_lists = manifest["bots"][0]["commandLists"]
    # Two lists: one for all scopes (ping/help/run), one personal-only
    # (connect/whoami/disconnect).
    assert len(command_lists) == 2

    global_list = next(cl for cl in command_lists if set(cl["scopes"]) >= {"team", "groupchat"})
    global_titles = [c["title"] for c in global_list["commands"]]
    assert "ping" in global_titles
    assert "help" in global_titles
    # Use ``startswith`` because the title is "run <agent-name> <json-input>".
    assert any(t.startswith("run ") for t in global_titles)

    dm_list = next(cl for cl in command_lists if cl["scopes"] == ["personal"])
    dm_titles = [c["title"] for c in dm_list["commands"]]
    assert any(t.startswith("connect") for t in dm_titles)
    assert "whoami" in dm_titles
    assert "disconnect" in dm_titles


@pytest.mark.unit
def test_manifest_developer_block_has_required_urls(manifest: dict) -> None:
    """Teams' validation rejects manifests without a developer block —
    name + website + privacy + terms are all required, even for
    sideloaded apps."""
    dev = manifest["developer"]
    assert dev["name"]
    assert dev["websiteUrl"].startswith("https://")
    assert dev["privacyUrl"].startswith("https://")
    assert dev["termsOfUseUrl"].startswith("https://")


@pytest.mark.unit
def test_manifest_icons_paths_match_filesystem(manifest: dict) -> None:
    """Icon paths in the manifest must point at files that actually
    exist in the appPackage dir — the zipper would silently produce
    a broken zip otherwise."""
    color = _APP_PACKAGE_DIR / manifest["icons"]["color"]
    outline = _APP_PACKAGE_DIR / manifest["icons"]["outline"]
    assert color.is_file(), f"missing icon: {color}"
    assert outline.is_file(), f"missing icon: {outline}"


# ---------------------------------------------------------------------------
# scripts/teams-package.sh
# ---------------------------------------------------------------------------


def _run_zipper(*, env: dict | None = None, output: Path | None = None) -> tuple[int, str, str]:
    """Run the zipper script in a clean env. Returns (returncode, stdout, stderr)."""
    full_env = dict(os.environ)
    if env is not None:
        full_env.update(env)
    cmd = [str(_ZIPPER_SCRIPT)]
    if output is not None:
        cmd.extend(["--output", str(output)])
    # ``check=False`` because tests assert on non-zero exits too (the
    # placeholder-warning test expects 0 with a stderr warning, not a
    # crash). Calling subprocess.run without check=False is a ruff
    # nuisance, not a real safety issue here — every caller checks the
    # returncode explicitly.
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=full_env, cwd=_REPO_ROOT, check=False
    )
    return result.returncode, result.stdout, result.stderr


@pytest.mark.unit
def test_zipper_produces_valid_zip_with_correct_layout(tmp_path: Path) -> None:
    """Default invocation builds a zip with manifest.json + icons/ at
    the root (Teams Admin Center requires no nested dirs)."""
    out = tmp_path / "movate-teams.zip"
    code, _, _ = _run_zipper(output=out)
    assert code == 0
    assert out.exists()

    with zipfile.ZipFile(out) as zf:
        names = sorted(zf.namelist())
    assert "manifest.json" in names
    # Either ``icons/color.png`` directly or ``icons/`` + ``icons/color.png``,
    # depending on whether the zip preserves dir entries. The script uses
    # ``zip -qr`` which does emit the dir entry — accept both.
    assert any(n.endswith("icons/color.png") for n in names)
    assert any(n.endswith("icons/outline.png") for n in names)


@pytest.mark.unit
def test_zipper_substitutes_app_id_from_env(tmp_path: Path) -> None:
    """When MOVATE_TEAMS_BOT_APP_ID is set, the manifest in the
    output zip carries that id (NOT the placeholder)."""
    custom_id = "11111111-2222-3333-4444-555555555555"
    out = tmp_path / "movate-teams.zip"
    code, _, _ = _run_zipper(env={"MOVATE_TEAMS_BOT_APP_ID": custom_id}, output=out)
    assert code == 0

    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert manifest["id"] == custom_id
    assert manifest["bots"][0]["botId"] == custom_id


@pytest.mark.unit
def test_zipper_warns_on_placeholder_app_id(tmp_path: Path) -> None:
    """The placeholder UUID is intentional for local-dev builds but
    rejected by Teams Admin Center. The warning is operator-friendly —
    not a hard failure (so the build can still happen for testing)."""
    out = tmp_path / "movate-teams.zip"
    # Unset MOVATE_TEAMS_BOT_APP_ID even if the test runner has it.
    env = {"MOVATE_TEAMS_BOT_APP_ID": ""}
    code, _, stderr = _run_zipper(env=env, output=out)
    assert code == 0
    assert "MOVATE_TEAMS_BOT_APP_ID is unset" in stderr
    # The zip still builds (warning, not error).
    assert out.exists()


@pytest.mark.unit
def test_zipper_substitutes_valid_domains(tmp_path: Path) -> None:
    """validDomains takes a comma-separated env value and lands as a
    JSON array in the manifest."""
    out = tmp_path / "movate-teams.zip"
    code, _, _ = _run_zipper(
        env={
            "MOVATE_TEAMS_BOT_APP_ID": "11111111-2222-3333-4444-555555555555",
            "MOVATE_TEAMS_VALID_DOMAINS": "movate.com, langfuse.movate.com ,trace.dev",
        },
        output=out,
    )
    assert code == 0
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    # Whitespace trimmed; empties dropped.
    assert manifest["validDomains"] == [
        "movate.com",
        "langfuse.movate.com",
        "trace.dev",
    ]


@pytest.mark.unit
def test_zipper_overrides_version_from_env(tmp_path: Path) -> None:
    out = tmp_path / "movate-teams.zip"
    code, _, _ = _run_zipper(
        env={
            "MOVATE_TEAMS_BOT_APP_ID": "11111111-2222-3333-4444-555555555555",
            "MOVATE_TEAMS_BOT_VERSION": "0.7.99-rc1",
        },
        output=out,
    )
    assert code == 0
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert manifest["version"] == "0.7.99-rc1"


@pytest.mark.unit
def test_zipper_leaves_committed_manifest_unchanged(tmp_path: Path) -> None:
    """The script copies manifest.json into a temp dir before
    substituting — the committed file in appPackage/ must not be
    mutated regardless of env vars passed."""
    before = _MANIFEST_PATH.read_text()
    out = tmp_path / "movate-teams.zip"
    _run_zipper(
        env={"MOVATE_TEAMS_BOT_APP_ID": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
        output=out,
    )
    after = _MANIFEST_PATH.read_text()
    assert before == after, "zipper script mutated the committed manifest.json"


@pytest.mark.unit
def test_zipper_warns_on_placeholder_icons(tmp_path: Path) -> None:
    """Cheap heuristic: a < 1KB color.png looks like the committed
    placeholder, not a real production icon. Warning surfaces in
    stderr."""
    out = tmp_path / "movate-teams.zip"
    code, _, stderr = _run_zipper(output=out)
    assert code == 0
    assert "placeholder" in stderr.lower()
