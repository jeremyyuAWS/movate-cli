"""User config round-trip + target/token resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.user_config import (
    TargetConfig,
    UserConfig,
    UserConfigError,
    config_path,
    load_user_config,
    resolve_bearer_token,
    resolve_target,
    save_user_config,
)


@pytest.mark.unit
def test_load_missing_file_returns_empty(tmp_path: Path, monkeypatch) -> None:
    """First-run UX: missing config file is not an error, just an empty config."""
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "never-exists.yaml"))
    cfg = load_user_config()
    assert cfg.targets == {}
    assert cfg.active is None


@pytest.mark.unit
def test_save_then_load_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    cfg = UserConfig(
        targets={
            "prod": TargetConfig(url="https://prod.example.com", key_env="PROD_KEY"),
            "local": TargetConfig(url="http://127.0.0.1:8000", key_env="LOCAL_KEY"),
        },
        active="prod",
    )
    save_user_config(cfg)
    assert config_path().exists()
    loaded = load_user_config()
    assert loaded.active == "prod"
    assert "prod" in loaded.targets
    assert loaded.targets["prod"].url == "https://prod.example.com"
    assert loaded.targets["local"].key_env == "LOCAL_KEY"


@pytest.mark.unit
def test_load_rejects_malformed_yaml(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text("this is: not: valid: yaml: : :")
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(p))
    with pytest.raises(UserConfigError, match="YAML"):
        load_user_config()


@pytest.mark.unit
def test_load_rejects_unknown_top_level_keys(tmp_path: Path, monkeypatch) -> None:
    """Extra keys catch typos at parse time, not deep in a CLI command."""
    p = tmp_path / "cfg.yaml"
    p.write_text("targets: {}\nactive: null\nrogue_field: hello\n")
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(p))
    with pytest.raises(UserConfigError):
        load_user_config()


@pytest.mark.unit
def test_resolve_target_uses_active_when_name_omitted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    save_user_config(
        UserConfig(
            targets={"prod": TargetConfig(url="https://prod", key_env="P")},
            active="prod",
        )
    )
    name, target = resolve_target(None)
    assert name == "prod"
    assert target.url == "https://prod"


@pytest.mark.unit
def test_resolve_target_errors_when_no_active(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    # Config exists but no active target.
    save_user_config(UserConfig(targets={"prod": TargetConfig(url="https://prod", key_env="P")}))
    with pytest.raises(UserConfigError, match="no --target"):
        resolve_target(None)


@pytest.mark.unit
def test_resolve_target_errors_for_unknown_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    save_user_config(
        UserConfig(
            targets={"prod": TargetConfig(url="https://prod", key_env="P")},
            active="prod",
        )
    )
    with pytest.raises(UserConfigError, match="not found"):
        resolve_target("ghost")


@pytest.mark.unit
def test_resolve_bearer_token_reads_env_var(monkeypatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "secret-bearer-value")
    target = TargetConfig(url="https://x", key_env="MY_TOKEN")
    assert resolve_bearer_token(target) == "secret-bearer-value"


@pytest.mark.unit
def test_resolve_bearer_token_errors_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("ABSENT_TOKEN", raising=False)
    target = TargetConfig(url="https://x", key_env="ABSENT_TOKEN")
    with pytest.raises(UserConfigError, match="unset or empty"):
        resolve_bearer_token(target)


@pytest.mark.unit
def test_resolve_bearer_token_errors_on_empty_string(monkeypatch) -> None:
    monkeypatch.setenv("EMPTY_TOKEN", "")
    target = TargetConfig(url="https://x", key_env="EMPTY_TOKEN")
    with pytest.raises(UserConfigError, match="unset or empty"):
        resolve_bearer_token(target)


# ---------------------------------------------------------------------------
# TargetConfig — optional Azure deploy fields (used by ``movate deploy``)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_config_azure_fields_default_to_none() -> None:
    """A target registered without --azure-* flags reads back with all
    Azure fields as None — read-only / non-deployable targets are valid."""
    t = TargetConfig(url="https://prod", key_env="K")
    assert t.azure_subscription is None
    assert t.azure_resource_group is None
    assert t.azure_acr_name is None
    assert t.azure_env is None


@pytest.mark.unit
def test_target_config_azure_fields_round_trip(tmp_path: Path, monkeypatch) -> None:
    """All four Azure fields survive a save → load cycle so ``movate deploy``
    can find them later."""
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    cfg = UserConfig(
        targets={
            "prod": TargetConfig(
                url="https://prod",
                key_env="K",
                azure_subscription="sub-id",
                azure_resource_group="movate-prod-rg",
                azure_acr_name="movateprodacr",
                azure_env="prod",
            )
        },
        active="prod",
    )
    save_user_config(cfg)
    loaded = load_user_config()
    t = loaded.targets["prod"]
    assert t.azure_subscription == "sub-id"
    assert t.azure_resource_group == "movate-prod-rg"
    assert t.azure_acr_name == "movateprodacr"
    assert t.azure_env == "prod"


@pytest.mark.unit
def test_target_config_round_trip_omits_none_fields(tmp_path: Path, monkeypatch) -> None:
    """The persisted YAML doesn't carry ``null`` Azure fields — keeps the
    file readable for the common (deploy-less) target case."""
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    cfg = UserConfig(
        targets={"local": TargetConfig(url="http://127.0.0.1:8000", key_env="L")},
        active="local",
    )
    path = save_user_config(cfg)
    content = path.read_text()
    assert "azure_subscription" not in content
    assert "azure_resource_group" not in content


@pytest.mark.unit
def test_cli_config_add_target_with_azure_flags_persists(tmp_path: Path, monkeypatch) -> None:
    """``movate config add-target --azure-...`` writes the Azure fields
    so ``movate deploy --target <name>`` sees them later."""
    from typer.testing import CliRunner  # noqa: PLC0415

    from movate.cli.main import app as cli_app  # noqa: PLC0415

    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    cli_runner = CliRunner(mix_stderr=False)

    result = cli_runner.invoke(
        cli_app,
        [
            "config",
            "add-target",
            "prod",
            "--url",
            "https://prod.example.com",
            "--key-env",
            "MOVATE_PROD_KEY",
            "--azure-subscription",
            "sub-id",
            "--azure-resource-group",
            "movate-prod-rg",
            "--azure-acr",
            "movateprodacr",
            "--azure-env",
            "prod",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Operator hint surfaces the fact that deploy is now enabled.
    assert "deploy enabled" in result.stderr

    cfg = load_user_config()
    t = cfg.targets["prod"]
    assert t.azure_subscription == "sub-id"
    assert t.azure_resource_group == "movate-prod-rg"
    assert t.azure_acr_name == "movateprodacr"
    assert t.azure_env == "prod"


@pytest.mark.unit
def test_cli_config_add_target_without_azure_flags_hints_deploy_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    """Without --azure-* flags, ``add-target`` works but flags the target
    as non-deployable — operator sees this at registration time, not
    later from a confusing ``movate deploy`` error."""
    from typer.testing import CliRunner  # noqa: PLC0415

    from movate.cli.main import app as cli_app  # noqa: PLC0415

    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    cli_runner = CliRunner(mix_stderr=False)

    result = cli_runner.invoke(
        cli_app,
        [
            "config",
            "add-target",
            "local",
            "--url",
            "http://127.0.0.1:8000",
            "--key-env",
            "MOVATE_LOCAL_KEY",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "deploy NOT enabled" in result.stderr
