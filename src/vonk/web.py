"""Approve page (GET/POST /c/<token>), /health and /metrics.

- ``GET /c/<token>`` renders the ChangeSet and has no side effects.
- ``POST /c/<token>`` (form: ``changeset_hash`` + ticked ``op`` indexes) runs
  ``Service.approve``: 200 applied, 404 unknown, 410 used/expired/superseded, 409 changed.
  Bodies over ``MAX_FORM_BYTES`` get 413 before parsing; the token is left untouched.
- ``GET /health`` is 200 whenever the process runs and the DB answers; no external calls.
- ``GET /metrics`` is the Prometheus exposition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from vonk.api import Service
from vonk.diff import possible_conflicts
from vonk.metrics import CONTENT_TYPE
from vonk.models import ChangeSet, Event, Operation, OpFlag, OpKind
from vonk.normalize import render_title

_ENV = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)

# (flag, label, hint), checked in this order; the first matching flag decides the group.
_FLAG_GROUPS = (
    (OpFlag.ORPHAN, "Weesevents", "Eigen events zonder herkomst. Alleen verwijderen als je ze "
     "aanvinkt."),
    (OpFlag.MANUALLY_EDITED, "Met de hand aangepast", "Je hebt deze events zelf gewijzigd; "
     "ze blijven staan tenzij je ze aanvinkt."),
    (OpFlag.MOVED, "Verplaatst", "Zelfde activiteit op een andere datum."),
    (OpFlag.RENAME_ONLY, "Enkel nieuwe titel", "Alleen de titel verandert (kindnaam)."),
)  # fmt: skip
_KIND_GROUPS = {
    OpKind.ADD: "Nieuw",
    OpKind.UPDATE: "Gewijzigd",
    OpKind.REMOVE: "Verwijderd",
}
_UNTICKED = (OpFlag.ORPHAN, OpFlag.MANUALLY_EDITED)
MAX_FORM_BYTES = 64 * 1024


def _when(ev: Event) -> str:
    day = ev.date.strftime("%a %d/%m/%Y")
    if ev.all_day:
        return day + (f" t.e.m. {ev.end_date:%d/%m/%Y}" if ev.end_date else " (hele dag)")
    end = f"–{ev.end:%H:%M}" if ev.end else ""
    return f"{day} {ev.start:%H:%M}{end}"


def _show(ev: Event | None, service: Service) -> dict[str, str] | None:
    if ev is None:
        return None
    title = render_title(ev, service.settings.child_names) if ev.scope_key else ev.title_activity
    return {"when": _when(ev), "title": title, "notes": ev.notes, "scope": ev.scope_key}


@dataclass
class _Group:
    label: str
    hint: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)


def _groups(cs: ChangeSet, related: tuple[Event, ...], service: Service) -> list[_Group]:
    groups: dict[str, _Group] = {}
    for index, op in enumerate(cs.ops):
        group = _group_of(op, groups)
        target = op.after or op.before
        conflicts = []
        if OpFlag.POSSIBLE_CONFLICT in op.flags and target is not None:
            conflicts = [_show(b, service) for _, b in possible_conflicts([target], related)]
        group.rows.append(
            {
                "index": index,
                "kind": OpKind(op.kind).value,
                "checked": not any(f in op.flags for f in _UNTICKED),
                "before": _show(op.before, service),
                "after": _show(op.after, service),
                "conflicts": conflicts,
            }
        )
    return list(groups.values())


def _group_of(op: Operation, groups: dict[str, _Group]) -> _Group:
    for flag, label, hint in _FLAG_GROUPS:
        if flag in op.flags:
            return groups.setdefault(label, _Group(label, hint))
    label = _KIND_GROUPS[OpKind(op.kind)]
    return groups.setdefault(label, _Group(label))


def _page(status: int, title: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("message", "")
    html = _ENV.get_template("changeset.html").render(title=title, status=status, **ctx)
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


def routes(service: Service) -> list[Route]:
    async def view(request: Request) -> Response:
        token = request.path_params["token"]
        out = await run_in_threadpool(service.view, token)
        if out.status != 200 or out.changeset is None:
            return _page(out.status, "vonk", message=out.message)
        cs = out.changeset
        return _page(
            200,
            f"Wijzigingen voor {cs.scope_key}",
            scope_key=cs.scope_key,
            changeset_hash=cs.content_hash(),
            groups=_groups(cs, out.related, service),
            warnings=[*cs.warnings, *out.ingest_warnings],
            mass_removal=any(OpFlag.MASS_REMOVAL in op.flags for op in cs.ops),
            needs_attention=out.needs_attention,
        )

    async def approve(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            if int(request.headers.get("content-length", "0")) > MAX_FORM_BYTES:
                return _page(413, "vonk", message="Formulier te groot.")
        except ValueError:
            return _page(400, "vonk", message="Ongeldige Content-Length.")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_FORM_BYTES:
                return _page(413, "vonk", message="Formulier te groot.")
        form = parse_qs(body.decode("utf-8", "replace"))
        changeset_hash = (form.get("changeset_hash") or [""])[0]
        try:
            selected = [int(v) for v in form.get("op", [])]
        except ValueError:
            return _page(400, "vonk", message="Ongeldige selectie.")
        out = await run_in_threadpool(service.approve, token, changeset_hash, selected)
        title = f"Resultaat voor {out.changeset.scope_key}" if out.changeset else "vonk"
        return _page(out.status, title, message=out.message, results=out.results)

    async def health(request: Request) -> Response:
        ok = await run_in_threadpool(service.store.ping)
        return JSONResponse(
            {"status": "ok", "db": ok, "degraded": service.degraded}, status_code=200
        )

    async def metrics(request: Request) -> Response:
        await run_in_threadpool(service.metrics.refresh, service.store)
        return Response(service.metrics.render(), media_type=CONTENT_TYPE)

    return [
        Route("/c/{token}", view, methods=["GET"]),
        Route("/c/{token}", approve, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
    ]
