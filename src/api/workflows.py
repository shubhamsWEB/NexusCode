"""
POST/GET /workflows — Workflow Automation Engine REST API.

Endpoints:
  POST /workflows                         — Create or update a workflow
  GET  /workflows                         — List all workflows
  GET  /workflows/{workflow_id}           — Get workflow definition + recent runs
  DELETE /workflows/{workflow_id}         — Delete a workflow
  POST /workflows/{workflow_id}/run       — Trigger a workflow run
  GET  /workflows/runs/{run_id}           — Get run status + step breakdown
  GET  /workflows/runs/{run_id}/stream    — SSE live stream of run progress
  POST /workflows/checkpoints/{cp_id}/respond — Human responds to a checkpoint
  POST /webhooks/{path}                       — Inbound webhook: trigger workflow by webhook_path

SSE event types (for /stream endpoint):
  {"type": "workflow_started",   "run_id": "...", "workflow": "..."}
  {"type": "step_started",       "run_id": "...", "step_id": "...", "role": "..."}
  {"type": "step_complete",      "run_id": "...", "step_id": "...", "tokens": N}
  {"type": "step_failed",        "run_id": "...", "step_id": "...", "error": "..."}
  {"type": "checkpoint_created", "run_id": "...", "checkpoint_id": "..."}
  {"type": "workflow_complete",  "run_id": "...", "tokens_total": N}
  {"type": "workflow_error",     "run_id": "...", "error": "..."}
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.utils.logging import get_secure_logger
from src.workflows.parser import WorkflowParseError

logger = get_secure_logger(__name__)
router = APIRouter(prefix="/workflows", tags=["workflows"])
webhook_router = APIRouter(prefix="/webhooks", tags=["workflow-webhooks"])

# Background tasks set to prevent GC
_background_tasks: set[asyncio.Task] = set()


# ── Request / Response Models ─────────────────────────────────────────────────


class CreateWorkflowRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    yaml_definition: str = Field(..., min_length=10)
    description: str = Field("", max_length=1000)


class TriggerRunRequest(BaseModel):
    payload: str | dict[str, Any] = Field(
        default="{}",
        description=(
            "Trigger payload — accepted as a JSON object OR a JSON-stringified string. "
            "Both forms work, so any log-monitoring / alerting system (Datadog, "
            "PagerDuty, OpsGenie, custom webhooks) can send its native payload "
            "without schema changes."
        ),
    )


class CheckpointResponseRequest(BaseModel):
    response: str = Field(..., min_length=1)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("", response_model=None)
async def create_workflow(req: CreateWorkflowRequest) -> JSONResponse:
    """Create or update a workflow definition (identified by name)."""
    try:
        from src.workflows.registry import create_or_update_workflow
        result = await create_or_update_workflow(
            name=req.name,
            yaml_definition=req.yaml_definition,
            description=req.description,
        )
        return JSONResponse(result, status_code=201)
    except WorkflowParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("create_workflow failed")
        raise HTTPException(status_code=500, detail="Failed to save workflow") from exc


@router.get("", response_model=None)
async def list_workflows(active_only: bool = True) -> JSONResponse:
    """List all workflow definitions with summary stats."""
    from src.workflows.registry import list_workflows as _list
    workflows = await _list(active_only=active_only)
    return JSONResponse(workflows)


@router.get("/runs", response_model=None)
async def list_runs(
    workflow_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> JSONResponse:
    """List recent workflow runs, optionally filtered by workflow_id."""
    from src.workflows.registry import list_runs as _list_runs
    runs = await _list_runs(workflow_id=workflow_id, limit=min(limit, 100), offset=offset)
    return JSONResponse(runs)


@router.get("/runs/{run_id}", response_model=None)
async def get_run(run_id: str) -> JSONResponse:
    """Get full run status including all step executions."""
    from src.workflows.registry import get_run as _get_run
    run = await _get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return JSONResponse(run)


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """
    SSE stream of a workflow run's live progress.
    Subscribes to Redis pub/sub for real-time events.
    Falls back to polling the DB if Redis is unavailable.
    """
    return StreamingResponse(
        _run_sse_generator(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{workflow_id}", response_model=None)
async def get_workflow(workflow_id: str) -> JSONResponse:
    """Get a workflow definition with its last 10 runs."""
    from src.workflows.registry import get_workflow as _get_wf
    from src.workflows.registry import list_runs
    wf = await _get_wf(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} not found")
    runs = await list_runs(workflow_id=workflow_id, limit=10)
    return JSONResponse({**wf, "recent_runs": runs})


@router.delete("/{workflow_id}", response_model=None)
async def delete_workflow(workflow_id: str) -> JSONResponse:
    """Delete a workflow and all its runs."""
    from src.workflows.registry import delete_workflow as _delete
    deleted = await _delete(workflow_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} not found")
    return JSONResponse({"deleted": True, "id": workflow_id})


@router.post("/{workflow_id}/run", response_model=None)
async def trigger_run(workflow_id: str, req: TriggerRunRequest) -> JSONResponse:
    """
    Trigger a workflow run asynchronously.
    Returns immediately with run_id — use GET /workflows/runs/{run_id} to check status,
    or GET /workflows/runs/{run_id}/stream for live SSE events.
    """
    from src.workflows.registry import get_workflow as _get_wf
    wf = await _get_wf(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} not found")
    if not wf.get("is_active", True):
        raise HTTPException(status_code=409, detail="Workflow is disabled")

    # Parse the workflow YAML to get the WorkflowDef
    from src.workflows.executor import WorkflowExecutor
    from src.workflows.parser import parse_workflow

    try:
        wf_def = parse_workflow(wf["yaml_definition"])
    except WorkflowParseError as exc:
        raise HTTPException(status_code=422, detail=f"Workflow parse error: {exc}") from exc

    # Normalise payload to dict — accepts both a raw object and a JSON string
    if isinstance(req.payload, dict):
        parsed_payload: dict[str, Any] = req.payload
    else:
        try:
            parsed_payload = json.loads(req.payload)
            if not isinstance(parsed_payload, dict):
                parsed_payload = {"value": parsed_payload}
        except (json.JSONDecodeError, TypeError):
            parsed_payload = {"raw": req.payload}

    # Create the run record first so we can return the run_id immediately
    from src.workflows.registry import create_run
    run_id = await create_run(
        workflow_id=workflow_id,
        workflow_name=wf["name"],
        trigger_payload=parsed_payload,
    )

    # Pass pre_created_run_id so executor reuses this record (avoids FK violation)
    executor = WorkflowExecutor(
        wf_def,
        parsed_payload,
        workflow_id=workflow_id,
        pre_created_run_id=run_id,
    )

    # Execute in background
    async def _run_bg():
        from src.events.bus import EventBus
        try:
            async for event in executor.stream():
                await EventBus.publish("nexus:workflow:updates", {"run_id": run_id, **event})
        except Exception as exc:
            logger.exception("background workflow run failed: %s", exc)

    task = asyncio.create_task(_run_bg())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return JSONResponse(
        {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "workflow_name": wf["name"],
            "status": "pending",
            "stream_url": f"/workflows/runs/{run_id}/stream",
        },
        status_code=202,
    )


@router.post("/checkpoints/{checkpoint_id}/respond", response_model=None)
async def respond_to_checkpoint(
    checkpoint_id: str,
    req: CheckpointResponseRequest,
) -> JSONResponse:
    """Submit a human response to a waiting checkpoint."""
    from src.workflows.registry import answer_checkpoint
    answered = await answer_checkpoint(checkpoint_id, req.response)
    if not answered:
        raise HTTPException(
            status_code=404,
            detail=f"Checkpoint {checkpoint_id!r} not found or already answered",
        )
    return JSONResponse({"answered": True, "checkpoint_id": checkpoint_id})


# ── SSE Generator ─────────────────────────────────────────────────────────────


async def _run_sse_generator(run_id: str):
    """
    Stream live events for a workflow run via Redis pub/sub.
    Falls back to polling when Redis is unavailable.
    """
    def _event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    # First, emit current state
    from src.workflows.registry import get_run
    run = await get_run(run_id)
    if not run:
        yield _event({"type": "error", "message": f"Run {run_id!r} not found"})
        return

    # If already terminal, just emit the final state
    if run["status"] in ("completed", "failed", "cancelled"):
        yield _event({"type": "run_snapshot", "run": run})
        yield _event({"type": f"workflow_{'complete' if run['status'] == 'completed' else 'error'}", "run_id": run_id, "status": run["status"]})
        return

    # Subscribe to Redis for live events
    from src.events.bus import EventBus

    try:
        async for event in EventBus.subscribe("nexus:workflow:updates"):
            if event.get("run_id") != run_id:
                continue
            yield _event(event)

            # Stop streaming once terminal event received
            if event.get("type") in ("workflow_complete", "workflow_error"):
                break

    except Exception as exc:
        logger.warning("SSE stream error for run %s: %s", run_id, exc)
        # Fallback: emit current state
        run = await get_run(run_id)
        if run:
            yield _event({"type": "run_snapshot", "run": run})


# ── Webhook Trigger Router ─────────────────────────────────────────────────────


@webhook_router.post("/{webhook_path:path}", response_model=None)
async def receive_webhook(webhook_path: str, request: Request) -> JSONResponse:
    """
    Receive an inbound webhook and trigger the matching workflow.

    The entire request body (JSON or raw) is forwarded as the trigger payload.
    Workflows are matched by the ``webhook_path`` field in their trigger config,
    e.g. a workflow with ``trigger.webhook_path: /webhooks/alerts`` is reached at
    ``POST /webhooks/alerts``.

    Returns HTTP 202 immediately with run_id — use
    ``GET /workflows/runs/{run_id}/stream`` for live progress.
    """
    from src.workflows.registry import get_workflow_by_webhook_path

    # Normalise path
    path = "/" + webhook_path.lstrip("/")

    wf = await get_workflow_by_webhook_path(path)
    if not wf:
        raise HTTPException(
            status_code=404,
            detail=f"No active webhook workflow found for path {path!r}",
        )

    # Parse body — accept JSON object, JSON array (wrapped), or raw text
    try:
        body = await request.json()
        if isinstance(body, dict):
            parsed_payload: dict[str, Any] = body
        else:
            parsed_payload = {"value": body}
    except Exception:
        raw = await request.body()
        parsed_payload = {"raw": raw.decode("utf-8", errors="replace")}

    # Reuse existing trigger logic
    from src.workflows.executor import WorkflowExecutor
    from src.workflows.parser import parse_workflow
    from src.workflows.registry import create_run

    try:
        wf_def = parse_workflow(wf["yaml_definition"])
    except WorkflowParseError as exc:
        raise HTTPException(status_code=422, detail=f"Workflow parse error: {exc}") from exc

    run_id = await create_run(
        workflow_id=wf["id"],
        workflow_name=wf["name"],
        trigger_payload=parsed_payload,
    )

    executor = WorkflowExecutor(
        wf_def,
        parsed_payload,
        workflow_id=wf["id"],
        pre_created_run_id=run_id,
    )

    async def _run_bg():
        from src.events.bus import EventBus
        try:
            async for event in executor.stream():
                await EventBus.publish("nexus:workflow:updates", {"run_id": run_id, **event})
        except Exception as exc:
            logger.exception("webhook workflow run failed: %s", exc)

    task = asyncio.create_task(_run_bg())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info(
        "webhook triggered workflow=%r run_id=%s path=%s",
        wf["name"], run_id, path,
    )

    return JSONResponse(
        {
            "run_id": run_id,
            "workflow_id": wf["id"],
            "workflow_name": wf["name"],
            "status": "pending",
            "stream_url": f"/workflows/runs/{run_id}/stream",
        },
        status_code=202,
    )
