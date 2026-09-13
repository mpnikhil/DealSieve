"""FastAPI surface for DealSieve. Routes and payload shapes per docs/CONTRACTS.md.

Human mutation routes have two approver modes. When ``DEALSIEVE_APPROVER_TOKEN`` is
set, clients must send the same value in ``X-DealSieve-Approver`` and events record
``human:token``. When it is unset, only clients at ``127.0.0.1`` or ``::1`` are
accepted and events record ``human:local``.

`create_app(repo=None, policy=None, notifier=None)` builds the app; omitted dependencies are constructed
lazily (on first request) from the environment (DEALSIEVE_DB_PATH, DEALSIEVE_POLICY_PATH, DEALSIEVE_NOTIFIER,
...). Building lazily keeps `import dealsieve.api.app` safe even while W1/W2/W3 stubs still raise
NotImplementedError -- the module-level `app` object below can always be imported; only *using* an
unfinished dependency at request time fails.

Every JSON body is produced from the pydantic contracts in `dealsieve.schemas` via `model_dump_json()` (or
a `TypeAdapter` for lists of them) so `Money`/`Rate` fields serialize as plain numbers, never strings.
"""

from __future__ import annotations

import hmac
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, TypeAdapter

from dealsieve.ingestion.email import parse_eml
from dealsieve.ingestion.text import from_text
from dealsieve.memory import MemoryStore, get_memory_store
from dealsieve.notifications import Notifier, get_notifier
from dealsieve.outbound import Outbox, get_outbox
from dealsieve.persistence import Repo
from dealsieve.pipeline import process_inbound
from dealsieve.policy import InvestmentPolicy, load_policy
from dealsieve.schemas import (
    Channel,
    DiligenceRequest,
    InboundMessage,
    MemoryEvent,
    MemoryHit,
    Notification,
    Opportunity,
    OpportunityStatus,
    OutboundDraft,
    WatchlistItem,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

_LOCALHOST_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>DealSieve</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; color: #222;">
<h1>DealSieve API is running</h1>
<p>The dashboard has not been built yet (no <code>frontend/dist/index.html</code>).</p>
<p>Build the frontend, or use the API directly: <a href="/api/health">/api/health</a>,
<a href="/api/stats">/api/stats</a>, <a href="/api/opportunities">/api/opportunities</a>.</p>
</body>
</html>
"""

_watchlist_adapter: TypeAdapter[list[WatchlistItem]] = TypeAdapter(list[WatchlistItem])
_notification_adapter: TypeAdapter[list[Notification]] = TypeAdapter(list[Notification])
_draft_adapter: TypeAdapter[list[OutboundDraft]] = TypeAdapter(list[OutboundDraft])
_diligence_adapter: TypeAdapter[list[DiligenceRequest]] = TypeAdapter(list[DiligenceRequest])
_memory_hit_adapter: TypeAdapter[list[MemoryHit]] = TypeAdapter(list[MemoryHit])
_memory_event_adapter: TypeAdapter[list[MemoryEvent]] = TypeAdapter(list[MemoryEvent])


def _json(model: BaseModel, *, status_code: int = 200) -> Response:
    return Response(content=model.model_dump_json(), media_type="application/json", status_code=status_code)


def _json_list(adapter: TypeAdapter[Any], items: list[Any]) -> Response:
    return Response(content=adapter.dump_json(items), media_type="application/json")


def _decimal_to_number(obj: Any) -> Any:
    """Recursively turn Decimal into float so plain (non-Money/Rate) policy fields serialize as numbers."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_number(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_number(v) for v in obj]
    return obj


def _backend_name() -> str:
    try:
        from dealsieve.models.backend import backend_name

        return backend_name()
    except Exception:
        return os.environ.get("DEALSIEVE_MODEL_BACKEND", "unknown")


def _default_repo() -> Repo:
    db_path = Path(os.environ.get("DEALSIEVE_DB_PATH", "data/dealsieve.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    repo = Repo(db_path)
    repo.init_schema()
    return repo


def _opportunity_to_watchlist_item(opp: Opportunity) -> WatchlistItem:
    viability = opp.viability
    return WatchlistItem(
        opportunity_id=opp.opportunity_id,
        deal_number=opp.deal_number,
        display_name=opp.display_name,
        status=opp.status,
        current_asking_price=opp.current_asking_price,
        max_viable_price=viability.max_viable_price if viability else None,
        distance_pct=viability.distance_pct if viability else None,
        binding_constraints=viability.binding_constraints if viability else [],
        reason_summary=opp.reason_summary,
        updated_at=opp.updated_at,
    )


class IngestTextBody(BaseModel):
    text: str
    sender: str | None = None
    channel: Literal["telegram", "manual"] = "manual"


class FollowUpTickBody(BaseModel):
    as_of: str | None = None


class RejectDraftBody(BaseModel):
    reason: str | None = None


def _normalize_namespace(namespace: str | None) -> str:
    """"" (all), "investor"/"broker" (every principal/broker under that prefix), or a namespace as-is."""
    if not namespace:
        return ""
    if "/" in namespace:
        return namespace
    if namespace in {"investor", "broker"}:
        return f"{namespace}/*"
    return namespace


def require_human(request: Request) -> str:
    """Authenticate a human approver and return the auditable principal label."""
    configured = os.environ.get("DEALSIEVE_APPROVER_TOKEN")
    if configured is not None:
        supplied = request.headers.get("X-DealSieve-Approver")
        if supplied is None or not hmac.compare_digest(supplied, configured):
            raise HTTPException(status_code=401, detail="Valid approver token required")
        return "human:token"

    client_host = request.client.host if request.client is not None else None
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Approver action is limited to loopback clients")
    return "human:local"


def create_app(
    *,
    repo: Repo | None = None,
    policy: InvestmentPolicy | None = None,
    notifier: Notifier | None = None,
    outbox: Outbox | None = None,
    memory: MemoryStore | None = None,
) -> FastAPI:
    app = FastAPI(title="DealSieve", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_LOCALHOST_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Stored on app.state so dependencies can build them lazily (None means "not built yet").
    app.state.repo = repo
    app.state.policy = policy
    app.state.notifier = notifier
    app.state.outbox = outbox
    app.state.memory = memory

    def get_repo(request: Request) -> Repo:
        if request.app.state.repo is None:
            request.app.state.repo = _default_repo()
        return request.app.state.repo

    def get_policy(request: Request) -> InvestmentPolicy:
        if request.app.state.policy is None:
            request.app.state.policy = load_policy()
        return request.app.state.policy

    def get_notifier_dep(request: Request) -> Notifier:
        if request.app.state.notifier is None:
            request.app.state.notifier = get_notifier()
        return request.app.state.notifier

    def get_outbox_dep(request: Request, policy: InvestmentPolicy = Depends(get_policy)) -> Outbox:  # noqa: B008
        if request.app.state.outbox is None:
            request.app.state.outbox = get_outbox(policy)
        return request.app.state.outbox

    def get_memory_dep(request: Request, repo: Repo = Depends(get_repo)) -> MemoryStore:  # noqa: B008
        if request.app.state.memory is None:
            request.app.state.memory = get_memory_store(repo)
        return request.app.state.memory

    # ----------------------------------------------------------------------------------- health / stats

    @app.get("/api/health")
    def health(policy: InvestmentPolicy = Depends(get_policy)) -> JSONResponse:  # noqa: B008
        return JSONResponse(
            {"status": "ok", "backend": _backend_name(), "policy_version": policy.policy_version}
        )

    @app.get("/api/stats")
    def stats(repo: Repo = Depends(get_repo), policy: InvestmentPolicy = Depends(get_policy)) -> Response:  # noqa: B008
        return _json(repo.dashboard_stats(policy.policy_version))

    @app.get("/api/policy")
    def get_policy_route(policy: InvestmentPolicy = Depends(get_policy)) -> JSONResponse:  # noqa: B008
        return JSONResponse(_decimal_to_number(policy.model_dump(mode="python")))

    # ----------------------------------------------------------------------------------- opportunities

    @app.get("/api/opportunities")
    def list_opportunities(
        include_dead: bool = Query(default=False),
        repo: Repo = Depends(get_repo),  # noqa: B008
    ) -> Response:
        items = list(repo.watchlist())
        if include_dead:
            dead = [
                _opportunity_to_watchlist_item(o) for o in repo.list_opportunities(OpportunityStatus.DEAD)
            ]
            items = items + dead
        return _json_list(_watchlist_adapter, items)

    @app.get("/api/opportunities/{opportunity_id}")
    def get_opportunity_detail(
        opportunity_id: str,
        repo: Repo = Depends(get_repo),  # noqa: B008
        memory: MemoryStore = Depends(get_memory_dep),  # noqa: B008
    ) -> Response:
        detail = repo.opportunity_detail(opportunity_id, memory=memory)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No opportunity matching {opportunity_id!r}")
        return _json(detail)

    # ----------------------------------------------------------------------------------- drafts

    @app.get("/api/drafts")
    def list_drafts(status: str | None = Query(default=None), repo: Repo = Depends(get_repo)) -> Response:  # noqa: B008
        return _json_list(_draft_adapter, list(repo.list_drafts(status=status)))

    @app.post("/api/drafts/{draft_id}/approve")
    def approve_draft(
        draft_id: str,
        principal: str = Depends(require_human),  # noqa: B008
        repo: Repo = Depends(get_repo),  # noqa: B008
        policy: InvestmentPolicy = Depends(get_policy),  # noqa: B008
        outbox: Outbox = Depends(get_outbox_dep),  # noqa: B008
        memory: MemoryStore = Depends(get_memory_dep),  # noqa: B008
    ) -> Response:
        from dealsieve.diligence import approve_and_send

        try:
            return _json(
                approve_and_send(
                    draft_id,
                    repo=repo,
                    policy=policy,
                    outbox=outbox,
                    principal=principal,
                    memory=memory,
                )
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/drafts/{draft_id}/reject")
    def reject_draft(
        draft_id: str,
        # Plain `None` (not `Body(default=None)`): dealsieve/tests/model/world.py calls this
        # endpoint's closure directly, bypassing FastAPI's dependency injection, so any FastAPI
        # marker object used as a default would leak through unresolved. A bare Optional[BaseModel]
        # default is still recognized by FastAPI as an optional JSON body over real HTTP.
        body: RejectDraftBody | None = None,
        principal: str = Depends(require_human),  # noqa: B008
        repo: Repo = Depends(get_repo),  # noqa: B008
        memory: MemoryStore = Depends(get_memory_dep),  # noqa: B008
    ) -> Response:
        from dealsieve.diligence import reject_draft as reject_draft_action

        reason = body.reason if body is not None else None
        try:
            return _json(
                reject_draft_action(draft_id, repo=repo, principal=principal, reason=reason, memory=memory)
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ----------------------------------------------------------------------------------- notifications

    @app.get("/api/notifications")
    def list_notifications(
        opportunity_id: str | None = Query(default=None),
        repo: Repo = Depends(get_repo),  # noqa: B008
    ) -> Response:
        return _json_list(_notification_adapter, list(repo.list_notifications(opportunity_id=opportunity_id)))

    def _acknowledge_notification(
        notification_id: str,
        *,
        principal: str,
        repo: Repo,
        memory: MemoryStore,
        action: Literal["ignore", "review"],
    ) -> Notification:
        from dealsieve.diligence import acknowledge_opportunity

        notification = repo.get_notification(notification_id)
        if notification is None:
            raise HTTPException(status_code=404, detail=f"No notification {notification_id!r}")
        acknowledge_opportunity(
            notification.opportunity_id,
            repo=repo,
            principal=principal,
            memory=memory,
            notification=notification,
            action=action,
        )
        return notification

    @app.post("/api/notifications/{notification_id}/ignore")
    def ignore_notification(
        notification_id: str,
        principal: str = Depends(require_human),  # noqa: B008
        repo: Repo = Depends(get_repo),  # noqa: B008
        memory: MemoryStore = Depends(get_memory_dep),  # noqa: B008
    ) -> Response:
        return _json(
            _acknowledge_notification(notification_id, principal=principal, repo=repo, memory=memory, action="ignore")
        )

    @app.post("/api/notifications/{notification_id}/review")
    def review_notification(
        notification_id: str,
        principal: str = Depends(require_human),  # noqa: B008
        repo: Repo = Depends(get_repo),  # noqa: B008
        memory: MemoryStore = Depends(get_memory_dep),  # noqa: B008
    ) -> Response:
        return _json(
            _acknowledge_notification(notification_id, principal=principal, repo=repo, memory=memory, action="review")
        )

    # ----------------------------------------------------------------------------------- diligence

    @app.get("/api/diligence")
    def list_diligence(
        status: str | None = Query(default=None),
        repo: Repo = Depends(get_repo),  # noqa: B008
    ) -> Response:
        return _json_list(_diligence_adapter, list(repo.list_diligence_requests(status=status)))

    @app.post("/api/diligence/tick")
    def tick_diligence(
        body: FollowUpTickBody,
        _principal: str = Depends(require_human),  # noqa: B008
        repo: Repo = Depends(get_repo),  # noqa: B008
        policy: InvestmentPolicy = Depends(get_policy),  # noqa: B008
        notifier: Notifier = Depends(get_notifier_dep),  # noqa: B008
        outbox: Outbox = Depends(get_outbox_dep),  # noqa: B008
        memory: MemoryStore = Depends(get_memory_dep),  # noqa: B008
    ) -> JSONResponse:
        from dealsieve.diligence import run_follow_ups

        as_of = None
        if body.as_of:
            try:
                from datetime import datetime

                as_of = datetime.fromisoformat(body.as_of.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="as_of must be an ISO-8601 datetime") from exc
        report = run_follow_ups(
            repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=as_of, memory=memory
        )
        return JSONResponse(jsonable_encoder(report.model_dump(mode="json")))

    # ----------------------------------------------------------------------------------- memory

    @app.get("/api/memory")
    def list_memory(
        namespace: str | None = Query(default=None),
        q: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=200),
        memory: MemoryStore = Depends(get_memory_dep),  # noqa: B008
    ) -> Response:
        ns = _normalize_namespace(namespace)
        if q:
            namespaces = [ns] if ns else ["investor/*", "broker/*"]
            hits = memory.recall(q, namespaces=namespaces, limit=limit)
            return _json_list(_memory_hit_adapter, hits)
        return _json_list(_memory_event_adapter, memory.list(ns, limit=limit))

    @app.get("/api/correspondence/{opportunity_id}")
    def correspondence(opportunity_id: str, repo: Repo = Depends(get_repo)) -> JSONResponse:  # noqa: B008
        if repo.get_opportunity(opportunity_id) is None:
            raise HTTPException(status_code=404, detail=f"No opportunity {opportunity_id!r}")
        inbound: list[InboundMessage] = repo.list_inbound_messages(opportunity_id)
        outbound = repo.list_drafts(opportunity_id=opportunity_id)
        return JSONResponse(
            {
                "inbound": [message.model_dump(mode="json") for message in inbound],
                "outbound": [draft.model_dump(mode="json") for draft in outbound],
            }
        )

    @app.get("/api/documents/{analysis_id}/images/{index}")
    def document_image(analysis_id: str, index: int, repo: Repo = Depends(get_repo)) -> Response:  # noqa: B008
        analysis = next(
            (
                item
                for opportunity in repo.list_opportunities()
                for item in repo.list_document_analyses(opportunity.opportunity_id)
                if item.analysis_id == analysis_id
            ),
            None,
        )
        if analysis is None:
            raise HTTPException(status_code=404, detail=f"No document analysis {analysis_id!r}")
        if index < 1 or index > len(analysis.image_paths):
            raise HTTPException(status_code=404, detail=f"No image {index} for document {analysis_id!r}")
        configured_root = Path(os.environ.get("DEALSIEVE_DOCS_DIR", "data/documents"))
        unresolved_root = configured_root.absolute()
        docs_root = configured_root.resolve()
        unresolved = Path(analysis.image_paths[index - 1]).absolute()
        try:
            relative = unresolved.relative_to(unresolved_root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Stored image is unavailable") from exc
        cursor = unresolved_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise HTTPException(status_code=404, detail="Stored image is unavailable")
        image_path = unresolved.resolve()
        if docs_root not in image_path.parents or not image_path.is_file():
            raise HTTPException(status_code=404, detail="Stored image is unavailable")
        media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        return Response(image_path.read_bytes(), media_type=media_type)

    # ----------------------------------------------------------------------------------- ingestion

    @app.post("/api/ingest/email")
    async def ingest_email(
        request: Request,
        _principal: str = Depends(require_human),  # noqa: B008
        repo: Repo = Depends(get_repo),  # noqa: B008
        policy: InvestmentPolicy = Depends(get_policy),  # noqa: B008
        notifier: Notifier = Depends(get_notifier_dep),  # noqa: B008
        outbox: Outbox = Depends(get_outbox_dep),  # noqa: B008
    ) -> Response:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None:
                raise HTTPException(status_code=400, detail="multipart body must include a 'file' field")
            raw: bytes = await upload.read()  # type: ignore[union-attr]
        else:
            raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="empty request body")
        message = parse_eml(raw)
        outcome = process_inbound(message, repo=repo, policy=policy, notifier=notifier, outbox=outbox)
        return _json(outcome)

    @app.post("/api/ingest/text")
    async def ingest_text(
        body: IngestTextBody,
        _principal: str = Depends(require_human),  # noqa: B008
        repo: Repo = Depends(get_repo),  # noqa: B008
        policy: InvestmentPolicy = Depends(get_policy),  # noqa: B008
        notifier: Notifier = Depends(get_notifier_dep),  # noqa: B008
        outbox: Outbox = Depends(get_outbox_dep),  # noqa: B008
    ) -> Response:
        message = from_text(body.text, channel=Channel(body.channel), sender=body.sender)
        outcome = process_inbound(message, repo=repo, policy=policy, notifier=notifier, outbox=outbox)
        return _json(outcome)

    # ----------------------------------------------------------------------------------- static / SPA

    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    def _spa_response() -> HTMLResponse:
        index_file = FRONTEND_DIST / "index.html"
        if index_file.is_file():
            return HTMLResponse(index_file.read_text(encoding="utf-8"))
        return HTMLResponse(_PLACEHOLDER_HTML)

    @app.get("/", include_in_schema=False)
    def spa_root() -> HTMLResponse:
        return _spa_response()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> HTMLResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        return _spa_response()

    return app


app = create_app()
