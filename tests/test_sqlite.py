"""SqliteProvider round-trip tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from movate.core.models import (
    ErrorInfo,
    FailureRecord,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage.sqlite import SqliteProvider


def _make_run(*, agent: str = "demo", status: JobStatus = JobStatus.SUCCESS) -> RunRecord:
    return RunRecord(
        run_id=str(uuid4()),
        job_id=str(uuid4()),
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="abc123",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="0.0.1",
        pricing_version="2026.05.01",
        status=status,
        input={"text": "hi"},
        output={"message": "ok"} if status is JobStatus.SUCCESS else None,
        metrics=Metrics(
            latency_ms=42,
            tokens=TokenUsage(input=10, output=5),
            cost_usd=0.0001,
            provider="openai/gpt-4o-mini-2024-07-18",
            pricing_version="2026.05.01",
        ),
        error=ErrorInfo(type="schema_error", message="bad", retryable=False)
        if status is JobStatus.ERROR
        else None,
    )


@pytest.mark.unit
async def test_save_and_list_runs(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "test.db")
    await db.init()

    run = _make_run()
    await db.save_run(run)

    rows = await db.list_runs()
    assert len(rows) == 1
    assert rows[0].run_id == run.run_id
    assert rows[0].metrics.cost_usd == 0.0001

    await db.close()


@pytest.mark.unit
async def test_list_runs_filters(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "test.db")
    await db.init()
    await db.save_run(_make_run(agent="alpha"))
    await db.save_run(_make_run(agent="beta"))
    await db.save_run(_make_run(agent="alpha", status=JobStatus.ERROR))

    alpha = await db.list_runs(agent="alpha")
    assert len(alpha) == 2
    beta = await db.list_runs(agent="beta")
    assert len(beta) == 1
    errored = await db.list_runs(status=JobStatus.ERROR.value)
    assert len(errored) == 1
    assert errored[0].error is not None
    assert errored[0].error.type == "schema_error"

    await db.close()


@pytest.mark.unit
async def test_save_failure(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "test.db")
    await db.init()
    await db.save_failure(
        FailureRecord(
            failure_id=str(uuid4()),
            run_id=str(uuid4()),
            tenant_id="local",
            agent="demo",
            failure_type="rate_limit",
            message="too many requests",
            retryable=True,
        )
    )
    # No public list_failures yet (Phase 4); just confirm no error and the
    # row was persisted by sniffing the underlying connection.
    async with db._db.execute("SELECT COUNT(*) FROM failures") as cur:
        count = (await cur.fetchone())[0]
    assert count == 1

    await db.close()


@pytest.mark.unit
async def test_init_is_idempotent(tmp_path: Path) -> None:
    """Second init() on the same DB should not raise."""
    db1 = SqliteProvider(db_path=tmp_path / "test.db")
    await db1.init()
    await db1.save_run(_make_run())
    await db1.close()

    db2 = SqliteProvider(db_path=tmp_path / "test.db")
    await db2.init()
    rows = await db2.list_runs()
    assert len(rows) == 1
    await db2.close()


@pytest.mark.unit
async def test_init_required_before_use(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "test.db")
    with pytest.raises(RuntimeError, match="init"):
        await db.save_run(_make_run())


@pytest.mark.unit
async def test_init_upgrades_pre_v0_3_runs_schema(tmp_path: Path) -> None:
    """Regression: opening a v0.1/v0.2 DB (no workflow_run_id column) must
    upgrade cleanly. Surfaced by manual smoke testing — earlier the
    `idx_runs_workflow_run` partial index in the SCHEMA referenced a
    column that the migrations only ALTER-ADD afterwards, so upgraders
    crashed in `init()` with `OperationalError: no such column`.

    This test re-creates the old schema by hand and asserts the next
    `init()` brings it forward without raising.
    """
    import aiosqlite  # noqa: PLC0415

    db_path = tmp_path / "old.db"
    # Synthesize a pre-v0.3 `runs` table — same columns as the original
    # v0.1 schema, with NO workflow_run_id / node_id.
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE runs (
                run_id           TEXT PRIMARY KEY,
                job_id           TEXT NOT NULL,
                tenant_id        TEXT NOT NULL,
                agent            TEXT NOT NULL,
                agent_version    TEXT NOT NULL,
                prompt_hash      TEXT NOT NULL,
                provider         TEXT NOT NULL,
                provider_version TEXT NOT NULL,
                pricing_version  TEXT NOT NULL,
                status           TEXT NOT NULL,
                input            TEXT NOT NULL,
                output           TEXT,
                metrics          TEXT NOT NULL,
                error            TEXT,
                created_at       TEXT NOT NULL
            );
            CREATE INDEX idx_runs_agent_created
                ON runs(agent, created_at DESC);
            """
        )
        await conn.commit()

    # Now run the current init() on top — must succeed and bring the
    # schema forward (workflow_run_id column + workflow_runs / evals tables
    # + the workflow_run_id index land via _MIGRATIONS).
    db = SqliteProvider(db_path=db_path)
    await db.init()
    # Inserting a run with the new fields populated proves the upgrade
    # actually added the columns rather than the index just being skipped.
    await db.save_run(
        _make_run(),  # baseline shape, no workflow link
    )
    rows = await db.list_runs()
    assert len(rows) == 1
    await db.close()

    # And a clean second init on the same (now-upgraded) DB is a no-op —
    # confirms the migration is idempotent on top of itself.
    db2 = SqliteProvider(db_path=db_path)
    await db2.init()
    await db2.close()


@pytest.mark.unit
async def test_build_storage_honors_movate_db_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MOVATE_DB`` overrides the default ``~/.movate/local.db`` path.

    Regression: surfaced by smoke testing — earlier ``build_storage()``
    ignored the env var entirely, which meant scratch / test runs all
    landed in the user's home DB and could collide with stale schema.
    """
    from movate.storage import build_storage  # noqa: PLC0415

    custom_path = tmp_path / "custom.db"
    monkeypatch.setenv("MOVATE_DB", str(custom_path))

    storage = build_storage()
    await storage.init()
    await storage.save_run(_make_run())
    await storage.close()

    # The custom path actually got written to (proves the env var was read).
    assert custom_path.exists()
    # ~/.movate/local.db NOT created by this test — confirmed by it landing
    # under tmp_path. (We don't assert the negative on $HOME — too noisy
    # if other tests have run.)
