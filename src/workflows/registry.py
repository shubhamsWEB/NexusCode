"""
WorkflowRegistry — CRUD operations for workflow definitions and run tracking.
All methods use the shared AsyncSessionLocal from src/storage/db.py.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger
from src.workflows.models import WorkflowDef
from src.workflows.parser import parse_workflow

logger = get_secure_logger(__name__)


# ── Workflow Definitions ──────────────────────────────────────────────────────


async def create_or_update_workflow(
    name: str,
    yaml_definition: str,
    description: str = "",
) -> dict[str, Any]:
    """
    Create a new workflow or update (bump version) an existing one by name.
    Parses and validates the YAML before persisting.
    Returns the persisted workflow definition as a dict.
    """
    # Parse + validate first — raises WorkflowParseError on bad YAML
    wf_def: WorkflowDef = parse_workflow(yaml_definition)

    async with AsyncSessionLocal() as session:
        # Check if exists
        existing = (
            await session.execute(
                text("SELECT id, version FROM workflow_definitions WHERE name = :name"),
                {"name": name},
            )
        ).mappings().first()

        trigger_config = wf_def.trigger.model_dump()

        if existing:
            new_version = existing["version"] + 1
            await session.execute(
                text("""
                    UPDATE workflow_definitions
                    SET description = :desc,
                        yaml_definition = :yaml,
                        trigger_type = :trigger_type,
                        trigger_config = :trigger_config,
                        version = :version,
                        updated_at = NOW()
                    WHERE name = :name
                """),
                {
                    "name": name,
                    "desc": description or wf_def.description,
                    "yaml": yaml_definition,
                    "trigger_type": wf_def.trigger.type.value,
                    "trigger_config": json.dumps(trigger_config),
                    "version": new_version,
                },
            )
            wf_id = str(existing["id"])
        else:
            wf_id = str(uuid.uuid4())
            await session.execute(
                text("""
                    INSERT INTO workflow_definitions
                        (id, name, description, yaml_definition, trigger_type, trigger_config, version)
                    VALUES
                        (:id, :name, :desc, :yaml, :trigger_type, :trigger_config, 1)
                """),
                {
                    "id": wf_id,
                    "name": name,
                    "desc": description or wf_def.description,
                    "yaml": yaml_definition,
                    "trigger_type": wf_def.trigger.type.value,
                    "trigger_config": json.dumps(trigger_config),
                },
            )

        await session.commit()

    return await get_workflow(wf_id)


async def get_workflow(workflow_id: str) -> dict[str, Any] | None:
    """Fetch a single workflow definition by ID."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT id, name, description, yaml_definition,
                           trigger_type, trigger_config, version, is_active,
                           created_at, updated_at
                    FROM workflow_definitions WHERE id = :id
                """),
                {"id": workflow_id},
            )
        ).mappings().first()

    if not row:
        return None
    return _format_wf_row(dict(row))


async def get_workflow_by_name(name: str) -> dict[str, Any] | None:
    """Fetch a workflow by its unique name."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT id, name, description, yaml_definition,
                           trigger_type, trigger_config, version, is_active,
                           created_at, updated_at
                    FROM workflow_definitions WHERE name = :name
                """),
                {"name": name},
            )
        ).mappings().first()

    if not row:
        return None
    return _format_wf_row(dict(row))


async def get_workflow_by_webhook_path(webhook_path: str) -> dict[str, Any] | None:
    """Fetch a workflow whose trigger_config.webhook_path matches the given path."""
    # Normalise: ensure leading slash
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT id, name, description, yaml_definition,
                           trigger_type, trigger_config, version, is_active,
                           created_at, updated_at
                    FROM workflow_definitions
                    WHERE trigger_type = 'webhook'
                      AND is_active = TRUE
                      AND trigger_config->>'webhook_path' = :path
                """),
                {"path": webhook_path},
            )
        ).mappings().first()
    if not row:
        return None
    return _format_wf_row(dict(row))


async def list_workflows(active_only: bool = True) -> list[dict[str, Any]]:
    """List all workflow definitions."""
    async with AsyncSessionLocal() as session:
        sql = """
            SELECT wd.id, wd.name, wd.description, wd.trigger_type,
                   wd.trigger_config,
                   wd.version, wd.is_active, wd.created_at, wd.updated_at,
                   (SELECT COUNT(*) FROM workflow_runs wr WHERE wr.workflow_id = wd.id) AS total_runs,
                   (SELECT MAX(wr2.started_at) FROM workflow_runs wr2 WHERE wr2.workflow_id = wd.id) AS last_run_at,
                   (SELECT wr3.status FROM workflow_runs wr3 WHERE wr3.workflow_id = wd.id ORDER BY wr3.started_at DESC LIMIT 1) AS last_run_status
            FROM workflow_definitions wd
        """
        if active_only:
            sql += " WHERE wd.is_active = TRUE"
        sql += " ORDER BY wd.created_at DESC"

        rows = (await session.execute(text(sql))).mappings().all()

    return [_format_wf_list_row(dict(r)) for r in rows]


async def set_workflow_active(workflow_id: str, is_active: bool) -> bool:
    """Enable or disable a workflow. Returns True if found."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("UPDATE workflow_definitions SET is_active = :active, updated_at = NOW() WHERE id = :id"),
            {"id": workflow_id, "active": is_active},
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def delete_workflow(workflow_id: str) -> bool:
    """Hard-delete a workflow and all its runs. Returns True if found."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("DELETE FROM workflow_definitions WHERE id = :id"),
            {"id": workflow_id},
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0) > 0


# ── Workflow Runs ─────────────────────────────────────────────────────────────


async def create_run(
    workflow_id: str,
    workflow_name: str,
    trigger_payload: dict[str, Any],
) -> str:
    """Create a new workflow run. Returns the run ID."""
    run_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO workflow_runs (id, workflow_id, workflow_name, status, trigger_payload)
                VALUES (:id, :workflow_id, :name, 'pending', :payload)
            """),
            {
                "id": run_id,
                "workflow_id": workflow_id,
                "name": workflow_name,
                "payload": json.dumps(trigger_payload),
            },
        )
        await session.commit()
    return run_id


async def update_run_status(
    run_id: str,
    status: str,
    error_message: str | None = None,
    result: dict[str, Any] | None = None,
    total_tokens: int = 0,
) -> None:
    """Update a run's status and optionally its result."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE workflow_runs
                SET status = :status,
                    error_message = :error,
                    result = :result,
                    total_tokens_used = total_tokens_used + :tokens,
                    completed_at = CASE WHEN :is_terminal THEN NOW() ELSE completed_at END
                WHERE id = :id
            """),
            {
                "id": run_id,
                "status": status,
                "error": error_message,
                "result": json.dumps(result) if result else None,
                "tokens": total_tokens,
                "is_terminal": status in ("completed", "failed", "cancelled"),
            },
        )
        await session.commit()


async def update_run_context(run_id: str, context: dict[str, Any]) -> None:
    """Persist the current execution context snapshot."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE workflow_runs SET context = :ctx WHERE id = :id"),
            {"id": run_id, "ctx": json.dumps(context)},
        )
        await session.commit()


async def get_run(run_id: str) -> dict[str, Any] | None:
    """Fetch a run with all its step executions."""
    async with AsyncSessionLocal() as session:
        run = (
            await session.execute(
                text("""
                    SELECT id, workflow_id, workflow_name, status, trigger_payload,
                           context, started_at, completed_at, total_tokens_used,
                           error_message, result
                    FROM workflow_runs WHERE id = :id
                """),
                {"id": run_id},
            )
        ).mappings().first()

        if not run:
            return None

        steps = (
            await session.execute(
                text("""
                    SELECT id, step_id, status, agent_role, step_type,
                           input_context, output, tokens_used,
                           started_at, completed_at, retry_count, error_message
                    FROM step_executions
                    WHERE run_id = :run_id
                    ORDER BY started_at ASC NULLS LAST
                """),
                {"run_id": run_id},
            )
        ).mappings().all()

        checkpoints = (
            await session.execute(
                text("""
                    SELECT id, step_id, prompt, options, response,
                           status, created_at, answered_at, timeout_at
                    FROM human_checkpoints
                    WHERE run_id = :run_id
                    ORDER BY created_at ASC
                """),
                {"run_id": run_id},
            )
        ).mappings().all()

    return _format_run(dict(run), [dict(s) for s in steps], [dict(c) for c in checkpoints])


async def list_runs(
    workflow_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List recent workflow runs."""
    where = "WHERE 1=1"
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if workflow_id:
        where += " AND workflow_id = :workflow_id"
        params["workflow_id"] = workflow_id

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT id, workflow_id, workflow_name, status,
                           started_at, completed_at, total_tokens_used, error_message
                    FROM workflow_runs
                    {where}
                    ORDER BY started_at DESC
                    LIMIT :limit OFFSET :offset
                """),
                params,
            )
        ).mappings().all()

    return [_format_run_summary(dict(r)) for r in rows]


# ── Step Executions ───────────────────────────────────────────────────────────


async def upsert_step(
    run_id: str,
    step_id: str,
    status: str,
    step_type: str = "agent",
    agent_role: str | None = None,
    input_context: dict[str, Any] | None = None,
    output: Any = None,
    tokens_used: int = 0,
    error_message: str | None = None,
) -> None:
    """Create or update a step execution record."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO step_executions
                    (id, run_id, step_id, status, step_type, agent_role,
                     input_context, output, tokens_used, started_at, error_message)
                VALUES
                    (gen_random_uuid(), :run_id, :step_id, :status, :step_type, :role,
                     :input, :output, :tokens,
                     CASE WHEN :status = 'running' THEN NOW() ELSE NULL END,
                     :error)
                ON CONFLICT (run_id, step_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    output = COALESCE(EXCLUDED.output, step_executions.output),
                    tokens_used = step_executions.tokens_used + EXCLUDED.tokens_used,
                    completed_at = CASE WHEN EXCLUDED.status IN ('completed','failed','skipped')
                                        THEN NOW() ELSE step_executions.completed_at END,
                    error_message = COALESCE(EXCLUDED.error_message, step_executions.error_message)
            """),
            {
                "run_id": run_id,
                "step_id": step_id,
                "status": status,
                "step_type": step_type,
                "role": agent_role,
                "input": json.dumps(input_context or {}),
                "output": json.dumps(output) if output is not None else None,
                "tokens": tokens_used,
                "error": error_message,
            },
        )
        await session.commit()


# ── Human Checkpoints ─────────────────────────────────────────────────────────


async def create_checkpoint(
    run_id: str,
    step_id: str,
    prompt: str,
    options: list[str] | None = None,
    timeout_hours: int = 24,
) -> str:
    """Create a human checkpoint. Returns the checkpoint ID."""
    from datetime import timedelta

    cp_id = str(uuid.uuid4())
    timeout_at = datetime.now(UTC) + timedelta(hours=timeout_hours)

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO human_checkpoints
                    (id, run_id, step_id, prompt, options, status, timeout_at)
                VALUES (:id, :run_id, :step_id, :prompt, :options, 'waiting', :timeout_at)
            """),
            {
                "id": cp_id,
                "run_id": run_id,
                "step_id": step_id,
                "prompt": prompt,
                "options": json.dumps(options or []),
                "timeout_at": timeout_at,
            },
        )
        await session.commit()

    return cp_id


async def answer_checkpoint(checkpoint_id: str, response: str) -> bool:
    """Record a human response to a checkpoint. Returns True if found."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                UPDATE human_checkpoints
                SET response = :response, status = 'answered', answered_at = NOW()
                WHERE id = :id AND status = 'waiting'
            """),
            {"id": checkpoint_id, "response": response},
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def get_pending_checkpoints(run_id: str) -> list[dict[str, Any]]:
    """Get all waiting checkpoints for a run."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT id, step_id, prompt, options, created_at, timeout_at
                    FROM human_checkpoints
                    WHERE run_id = :run_id AND status = 'waiting'
                    ORDER BY created_at ASC
                """),
                {"run_id": run_id},
            )
        ).mappings().all()

    return [_format_checkpoint(dict(r)) for r in rows]


async def get_workflows_by_trigger_type(trigger_type: str) -> list[dict[str, Any]]:
    """Get all active workflows with a specific trigger type (for scheduler)."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT id, name, yaml_definition, trigger_config
                    FROM workflow_definitions
                    WHERE trigger_type = :trigger_type AND is_active = TRUE
                """),
                {"trigger_type": trigger_type},
            )
        ).mappings().all()

    return [dict(r) for r in rows]


# ── Formatters ────────────────────────────────────────────────────────────────


def _fmt_ts(v) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _format_wf_row(row: dict) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row.get("description") or "",
        "yaml_definition": row["yaml_definition"],
        "trigger_type": row["trigger_type"],
        "trigger_config": row["trigger_config"] if isinstance(row["trigger_config"], dict) else json.loads(row["trigger_config"] or "{}"),
        "version": row["version"],
        "is_active": row["is_active"],
        "created_at": _fmt_ts(row.get("created_at")),
        "updated_at": _fmt_ts(row.get("updated_at")),
    }


def _format_wf_list_row(row: dict) -> dict[str, Any]:
    tc = row.get("trigger_config")
    if isinstance(tc, str):
        try:
            tc = json.loads(tc)
        except Exception:
            tc = {}
    tc = tc or {}
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row.get("description") or "",
        "trigger_type": row["trigger_type"],
        "webhook_path": tc.get("webhook_path"),
        "version": row["version"],
        "is_active": row["is_active"],
        "total_runs": row.get("total_runs") or 0,
        "last_run_at": _fmt_ts(row.get("last_run_at")),
        "last_run_status": row.get("last_run_status"),
        "created_at": _fmt_ts(row.get("created_at")),
        "updated_at": _fmt_ts(row.get("updated_at")),
    }


def _format_run(run: dict, steps: list[dict], checkpoints: list[dict] | None = None) -> dict[str, Any]:
    def _fmt_step(s: dict) -> dict:
        return {
            "id": str(s["id"]),
            "step_id": s["step_id"],
            "status": s["status"],
            "agent_role": s.get("agent_role"),
            "step_type": s.get("step_type", "agent"),
            "tokens_used": s.get("tokens_used") or 0,
            "output": s["output"] if isinstance(s["output"], dict) else json.loads(s["output"] or "null"),
            "started_at": _fmt_ts(s.get("started_at")),
            "completed_at": _fmt_ts(s.get("completed_at")),
            "retry_count": s.get("retry_count") or 0,
            "error_message": s.get("error_message"),
        }

    def _fmt_cp(c: dict) -> dict:
        return {
            "id": str(c["id"]),
            "step_id": c["step_id"],
            "prompt": c["prompt"],
            "options": c["options"] if isinstance(c["options"], list) else json.loads(c["options"] or "[]"),
            "response": c.get("response"),
            "status": c["status"],
            "created_at": _fmt_ts(c.get("created_at")),
            "answered_at": _fmt_ts(c.get("answered_at")),
            "timeout_at": _fmt_ts(c.get("timeout_at")),
        }

    return {
        "id": str(run["id"]),
        "workflow_id": str(run["workflow_id"]),
        "workflow_name": run["workflow_name"],
        "status": run["status"],
        "trigger_payload": run["trigger_payload"] if isinstance(run["trigger_payload"], dict) else json.loads(run["trigger_payload"] or "{}"),
        "started_at": _fmt_ts(run.get("started_at")),
        "completed_at": _fmt_ts(run.get("completed_at")),
        "total_tokens_used": run.get("total_tokens_used") or 0,
        "error_message": run.get("error_message"),
        "result": run["result"] if isinstance(run["result"], dict) else json.loads(run["result"] or "null"),
        "steps": [_fmt_step(s) for s in steps],
        "checkpoints": [_fmt_cp(c) for c in (checkpoints or [])],
    }


def _format_run_summary(run: dict) -> dict[str, Any]:
    return {
        "id": str(run["id"]),
        "workflow_id": str(run["workflow_id"]),
        "workflow_name": run["workflow_name"],
        "status": run["status"],
        "started_at": _fmt_ts(run.get("started_at")),
        "completed_at": _fmt_ts(run.get("completed_at")),
        "total_tokens_used": run.get("total_tokens_used") or 0,
        "error_message": run.get("error_message"),
    }


def _format_checkpoint(cp: dict) -> dict[str, Any]:
    return {
        "id": str(cp["id"]),
        "step_id": cp["step_id"],
        "prompt": cp["prompt"],
        "options": cp["options"] if isinstance(cp["options"], list) else json.loads(cp["options"] or "[]"),
        "created_at": _fmt_ts(cp.get("created_at")),
        "timeout_at": _fmt_ts(cp.get("timeout_at")),
    }
