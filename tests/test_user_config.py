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
