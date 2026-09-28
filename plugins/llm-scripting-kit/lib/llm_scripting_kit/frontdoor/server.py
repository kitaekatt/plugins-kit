"""Implementation of the llm-scripting-kit OpenAI-compatible front door."""
import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..account import probe_entry
from ..completion.types import (
    COST_SOURCE_PROVIDER,
    COST_SOURCE_REGISTRY_UNMETERED,
    valid_reported_cost,
)
from ..model_endpoints import (
    EndpointEntry,
    EndpointRegistry,
    EndpointRegistryError,
    TRANSPORT_KIND,
    load_endpoint_registry,
)


@dataclass
class RuntimeDeployment:
    entry: EndpointEntry
    in_flight: int = 0
    users: list[Optional[str]] = field(default_factory=list)
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None

    @property
    def routing(self) -> Any:
        return self.entry.routing


#: ``GET /health/backends?budget_ms=<int>``: the per-backend probe budget the
#: caller asks for, in integer milliseconds. Clamped to the maximum; a missing,
#: non-integer or non-positive value uses the default.
BACKEND_BUDGET_DEFAULT_MS = 1500
BACKEND_BUDGET_MAX_MS = 4000
BACKEND_HEALTH_PROTOCOL = 1


def _parse_budget_ms(raw: Optional[str]) -> int:
    """The per-backend probe budget in ms, from the raw query value."""
    try:
        value = int(raw) if raw is not None and raw.strip().lstrip("+-").isdigit() else None
    except ValueError:
        value = None
    if value is None or value <= 0:
        return BACKEND_BUDGET_DEFAULT_MS
    return min(value, BACKEND_BUDGET_MAX_MS)


def _group_verdict(statuses: list[str]) -> str:
    """reachable if any deployment is; unreachable only if every one decisively is."""
    if "reachable" in statuses:
        return "reachable"
    if statuses and all(status == "unreachable" for status in statuses):
        return "unreachable"
    return "unknown"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(registry_path: Optional[str]) -> EndpointRegistry:
    if registry_path is None:
        return load_endpoint_registry()
    env = dict(os.environ)
    env["MODEL_ENDPOINTS_REGISTRY"] = registry_path
    return load_endpoint_registry(env)


def _transport_entries(registry: EndpointRegistry) -> list[EndpointEntry]:
    return [
        entry for entry in registry.entries.values()
        if entry.kind == TRANSPORT_KIND and entry.routing is not None
    ]


def _groups(registry: EndpointRegistry) -> dict[str, list[RuntimeDeployment]]:
    result: dict[str, list[RuntimeDeployment]] = {}
    for entry in _transport_entries(registry):
        assert entry.routing is not None
        result.setdefault(entry.routing.group, []).append(RuntimeDeployment(entry))
    for deployments in result.values():
        deployments.sort(key=lambda deployment: (deployment.routing.order, deployment.entry.id))
    return result


def _check_text(registry: EndpointRegistry) -> str:
    lines: list[str] = []
    groups = _groups(registry)
    for group, deployments in sorted(groups.items()):
        lines.append(f"{group}:")
        for deployment in deployments:
            routing = deployment.routing
            lines.append(
                f"  order={routing.order} max_parallel={routing.max_parallel or 'uncapped'} "
                f"{deployment.entry.id} -> {deployment.entry.model}"
            )
    untagged = [
        entry.id for entry in registry.entries.values()
        if entry.kind == TRANSPORT_KIND and entry.routing is None
    ]
    lines.append("untagged transport entries:")
    lines.extend(f"  {entry_id}" for entry_id in sorted(untagged))
    lines.extend(f"note: {note}" for note in registry.notes)
    return "\n".join(lines)


def _error(message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error"}}


def _header_value(entry: EndpointEntry) -> dict[str, str]:
    return {"x-frontdoor-deployment": entry.id}


def _usage_from_json(body: bytes) -> tuple[Optional[int], Optional[int]]:
    try:
        usage = json.loads(body).get("usage") or {}
    except (ValueError, TypeError):
        return None, None
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


def _apply_cost_policy(
    content: bytes, entry: EndpointEntry
) -> tuple[bytes, Optional[float], Optional[str]]:
    """Decide which reported cost this front door stands behind (amendment R1).

    The serving deployment's ``billing.mode`` is the only authority. An upstream
    ``usage.cost`` is forwarded ONLY for ``provider-reported``; otherwise it and
    any ``usage.cost_source`` are stripped. ``unmetered`` then injects an
    explicit ``usage.cost: 0.0`` with ``usage.cost_source: registry-unmetered``.
    Returns the (possibly rewritten) body plus the validated amount and source
    for the access log. A body that is not a JSON object passes through as is.
    """
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return content, None, None
    if not isinstance(body, dict):
        return content, None, None
    mode = entry.billing_mode
    usage = body.get("usage")
    if not isinstance(usage, dict):
        if mode != "unmetered":
            return content, None, None
        usage = {}
        body["usage"] = usage
    changed = "cost_source" in usage
    usage.pop("cost_source", None)
    if mode == "provider-reported":
        amount = valid_reported_cost(usage.get("cost"))
        source = COST_SOURCE_PROVIDER if amount is not None else None
    else:
        changed = "cost" in usage or changed
        usage.pop("cost", None)
        amount = source = None
        if mode == "unmetered":
            usage["cost"] = 0.0
            usage["cost_source"] = COST_SOURCE_REGISTRY_UNMETERED
            amount, source = 0.0, COST_SOURCE_REGISTRY_UNMETERED
            changed = True
    if not changed:
        return content, amount, source
    return json.dumps(body).encode("utf-8"), amount, source


def _normalize_body(body: dict[str, Any], entry: EndpointEntry) -> tuple[dict[str, Any], Optional[str]]:
    forwarded = dict(body)
    if isinstance(forwarded.get("chat_template_kwargs"), dict):
        forwarded["chat_template_kwargs"] = dict(forwarded["chat_template_kwargs"])
    user = forwarded.pop("user", None)
    forwarded["model"] = entry.model
    effort = forwarded.pop("reasoning_effort", None)
    if effort is None:
        template = forwarded.get("chat_template_kwargs")
        if isinstance(template, dict):
            effort = template.pop("reasoning_effort", None)
    if effort is not None and entry.routing is not None:
        style = entry.routing.effort_style
        if style == "ninfer" and effort == "high":
            # NInfer's menu is none|low|medium|xhigh and rejects "high" with a
            # 400; the remap is keyed on the ninfer style so a plain top-level
            # OpenAI-compatible server still receives what the caller sent.
            effort = "xhigh"
        if style in ("top-level", "ninfer"):
            forwarded["reasoning_effort"] = effort
        else:
            template = forwarded.get("chat_template_kwargs")
            if not isinstance(template, dict):
                template = {}
                forwarded["chat_template_kwargs"] = template
            template["reasoning_effort"] = effort
    return forwarded, user


_KEY_CACHE: dict[str, Optional[str]] = {}


def _deployment_key(deployment: RuntimeDeployment) -> Optional[str]:
    """Resolve a keyed deployment's credential once, through the plugin's
    layered lookup (env var, project .env, user .env, secrets key_file) --
    the server process on the hosting machine rarely has the env var
    exported, but the fleet's secrets layer usually has the file."""
    entry = deployment.entry
    if entry.id in _KEY_CACHE:
        return _KEY_CACHE[entry.id]
    key: Optional[str] = os.environ.get(entry.key_env or "") or None
    if key is None:
        try:
            from ..api_key import get_api_key  # noqa: PLC0415

            key = get_api_key(endpoint=entry.id).key
        except Exception:  # noqa: BLE001 -- a lookup defect degrades to keyless
            key = None
    _KEY_CACHE[entry.id] = key
    return key


def _append_access(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def create_app(
    registry: EndpointRegistry,
    *,
    spill_after: float = 0.0,
    access_log: Optional[Path] = None,
) -> Any:
    """Create the FastAPI app for one loaded registry."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, Response, StreamingResponse
    except ImportError as exc:  # pragma: no cover - exercised by environments without extra
        raise RuntimeError("frontdoor requires the 'frontdoor' extra") from exc

    groups = _groups(registry)
    condition = asyncio.Condition()
    log_path = access_log or (Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-scripting-kit" / "frontdoor-access.jsonl")

    async def choose(group: str, excluded: set[str]) -> RuntimeDeployment:
        deployments = groups[group]
        while True:
            by_order: dict[int, list[RuntimeDeployment]] = {}
            for deployment in deployments:
                if deployment.entry.id not in excluded:
                    by_order.setdefault(deployment.routing.order, []).append(deployment)
            orders = sorted(by_order)

            def _eligible(order: int) -> list[RuntimeDeployment]:
                return [
                    deployment for deployment in by_order[order]
                    if deployment.routing.max_parallel is None
                    or deployment.in_flight < deployment.routing.max_parallel
                ]

            for index, order in enumerate(orders):
                eligible = _eligible(order)
                if not eligible and index < len(orders) - 1 and spill_after > 0:
                    # Give this tier one bounded chance to free a slot before
                    # spilling; whatever happens, the walk then moves ON to
                    # the next tier rather than restarting here.
                    try:
                        async with condition:
                            await asyncio.wait_for(condition.wait(), spill_after)
                    except asyncio.TimeoutError:
                        pass
                    eligible = _eligible(order)
                if eligible:
                    return eligible[0]
            # Every tier is at its cap: wait for any release, then walk again.
            async with condition:
                await condition.wait()

    async def release(deployment: RuntimeDeployment, user: Optional[str]) -> None:
        async with condition:
            deployment.in_flight -= 1
            if user in deployment.users:
                deployment.users.remove(user)
            condition.notify_all()

    async def call_upstream(deployment: RuntimeDeployment, payload: dict[str, Any], request: Request) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("frontdoor requires the 'frontdoor' extra") from exc
        headers = {}
        if deployment.entry.key_env:
            key = _deployment_key(deployment)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        url = deployment.entry.base_url.rstrip("/") + "/chat/completions"
        # No read timeout: a completion legitimately takes minutes, and httpx's
        # 5 s default turned every slow tier into a spurious spill. The client's
        # own deadline is the bound; the front door only bounds the connect.
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=None)
        )
        if payload.get("stream"):
            return client, client.stream("POST", url, json=payload, headers=headers)
        try:
            response = await client.post(url, json=payload, headers=headers)
            return client, response
        except Exception:
            await client.aclose()
            raise

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> Any:
        started = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(_error("request body must be JSON"), status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(_error("request body must be an object"), status_code=400)
        group = body.get("model")
        if group not in groups:
            return JSONResponse(_error(f"unknown model group: {group}"), status_code=404)
        stream = bool(body.get("stream"))
        user = body.get("user")
        excluded: set[str] = set()
        deployment: Optional[RuntimeDeployment] = None
        status: Optional[int] = None
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None
        error: Optional[str] = None
        reported_cost: Optional[float] = None
        cost_source: Optional[str] = None
        while True:
            if all(deployment.entry.id in excluded for deployment in groups[group]):
                if deployment is not None:
                    _log(deployment, user, group, body, 502, started, prompt_tokens, completion_tokens, stream, error, log_path)
                return Response(
                    content=json.dumps(_error(error or "all deployments failed")),
                    status_code=502,
                    media_type="application/json",
                    headers=_header_value(deployment.entry) if deployment is not None else {},
                )
            deployment = await choose(group, excluded)
            payload, user = _normalize_body(body, deployment.entry)
            deployment.in_flight += 1
            deployment.users.append(user)
            acquired = True
            try:
                client, upstream = await call_upstream(deployment, payload, request)
                if stream:
                    context = upstream
                    response_context = await context.__aenter__()
                    status = response_context.status_code
                    if status >= 500 and deployment.entry.id not in excluded:
                        deployment.last_error = f"upstream status {status}"
                        deployment.last_error_at = _iso_now()
                        await context.__aexit__(None, None, None)
                        await client.aclose()
                        await release(deployment, user)
                        acquired = False
                        excluded.add(deployment.entry.id)
                        continue

                    async def chunks() -> Any:
                        nonlocal completion_tokens, prompt_tokens
                        try:
                            async for chunk in response_context.aiter_bytes():
                                for line in chunk.splitlines():
                                    if line.startswith(b"data: "):
                                        p, c = _usage_from_json(line[6:])
                                        prompt_tokens = p if p is not None else prompt_tokens
                                        completion_tokens = c if c is not None else completion_tokens
                                yield chunk
                        finally:
                            await context.__aexit__(None, None, None)
                            await client.aclose()
                            await release(deployment, user)
                            _log(deployment, user, group, body, status, started, prompt_tokens, completion_tokens, stream, error, log_path)

                    return StreamingResponse(chunks(), status_code=status, media_type=response_context.headers.get("content-type"), headers=_header_value(deployment.entry))
                response = upstream
                status = response.status_code
                content = response.content
                prompt_tokens, completion_tokens = _usage_from_json(content)
                if status >= 500 and deployment.entry.id not in excluded:
                    deployment.last_error = f"upstream status {status}"
                    deployment.last_error_at = _iso_now()
                    await client.aclose()
                    await release(deployment, user)
                    acquired = False
                    excluded.add(deployment.entry.id)
                    continue
                error = None
                if status < 400:
                    content, reported_cost, cost_source = _apply_cost_policy(
                        content, deployment.entry
                    )
                return Response(content=content, status_code=status, media_type=response.headers.get("content-type"), headers=_header_value(deployment.entry))
            except Exception as exc:
                # httpx timeouts stringify to "" -- keep the class name so the
                # log and /health say what actually happened.
                error = f"{type(exc).__name__}: {exc}".rstrip(": ")
                deployment.last_error = error
                deployment.last_error_at = _iso_now()
                if deployment.entry.id not in excluded:
                    excluded.add(deployment.entry.id)
                    await release(deployment, user)
                    acquired = False
                    continue
                raise
            finally:
                if not stream and acquired and deployment is not None:
                    await release(deployment, user)
                    _log(deployment, user, group, body, status, started, prompt_tokens, completion_tokens, stream, error, log_path, reported_cost, cost_source)

    @app.get("/v1/models")
    async def models() -> Any:
        return {"object": "list", "data": [{"id": group, "object": "model", "owned_by": "frontdoor"} for group in sorted(groups)]}

    @app.get("/health")
    async def health() -> Any:
        return {"status": "ok", "spill_after_s": spill_after, "deployments": [_deployment_json(d) for ds in groups.values() for d in ds]}

    @app.get("/health/backends")
    async def health_backends(budget_ms: Optional[str] = None) -> Any:
        """Per-deployment reachability of THIS app's registry (protocol 1).

        Issues only ``GET <deployment base>/models`` (never a completion), all
        deployments concurrently, each bounded by the explicit budget. The
        historical ``last_error`` is not a verdict and is not consulted.
        """
        timeout = _parse_budget_ms(budget_ms) / 1000.0

        async def probe(deployment: RuntimeDeployment) -> dict[str, Any]:
            entry = deployment.entry
            resolver = (lambda d=deployment: _deployment_key(d)) if entry.key_env else None
            try:
                result = await asyncio.to_thread(
                    probe_entry, entry, timeout=timeout, key_resolver=resolver
                )
                if result.ok:
                    status = "reachable"
                elif result.decisive and result.resolved:
                    status = "unreachable"
                else:
                    status = "unknown"
                detail = result.detail
            except Exception as exc:  # noqa: BLE001 -- a failed check is "unknown", never "down"
                status, detail = "unknown", f"{type(exc).__name__}: {exc}".rstrip(": ")
            return {"id": entry.id, "status": status, "checked": "models-endpoint", "detail": detail}

        ordered = [(group, d) for group, ds in groups.items() for d in ds]
        results = await asyncio.gather(*(probe(d) for _, d in ordered))
        report: dict[str, Any] = {}
        for (group, _), item in zip(ordered, results):
            report.setdefault(group, {"status": "unknown", "deployments": []})["deployments"].append(item)
        for value in report.values():
            value["status"] = _group_verdict([d["status"] for d in value["deployments"]])
        return {
            "protocol": BACKEND_HEALTH_PROTOCOL,
            "frontdoor_status": "ok",
            "checked_at": _iso_now(),
            "groups": report,
        }

    @app.get("/who")
    async def who() -> Any:
        return {"deployments": [_deployment_json(d, include_users=True) for ds in groups.values() for d in ds]}

    return app


def _deployment_json(deployment: RuntimeDeployment, *, include_users: bool = False) -> dict[str, Any]:
    routing = deployment.routing
    value = {"id": deployment.entry.id, "group": routing.group, "order": routing.order, "max_parallel": routing.max_parallel, "in_flight": deployment.in_flight, "last_error": deployment.last_error, "last_error_at": deployment.last_error_at}
    if include_users:
        value["users"] = list(deployment.users)
    return value


def _log(deployment: RuntimeDeployment, user: Optional[str], group: str, body: dict[str, Any], status: Optional[int], started: float, prompt_tokens: Optional[int], completion_tokens: Optional[int], stream: bool, error: Optional[str], path: Path, reported_cost: Optional[float] = None, cost_source: Optional[str] = None) -> None:
    routing = deployment.routing
    record: dict[str, Any] = {"ts": _iso_now(), "user": user, "group": group, "deployment": deployment.entry.id, "api_base": deployment.entry.base_url, "model": deployment.entry.model, "order": routing.order, "status": status, "latency_ms": int((time.monotonic() - started) * 1000), "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "stream": stream, "error": error}
    if reported_cost is not None and cost_source is not None:
        record["reported_cost_usd"] = reported_cost
        record["reported_cost_source"] = cost_source
    _append_access(path, record)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m llm_scripting_kit.frontdoor")
    parser.add_argument("--host", default=os.environ.get("FRONTDOOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FRONTDOOR_PORT", "4000")))
    parser.add_argument("--spill-after", type=float, default=0.0)
    parser.add_argument("--registry")
    parser.add_argument("--access-log")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = _load(args.registry)
    except EndpointRegistryError as exc:
        print(str(exc), file=os.sys.stderr)
        return 2
    if args.check:
        print(_check_text(registry))
        return 0
    if args.print_config:
        print(json.dumps({"host": args.host, "port": args.port, "spill_after_s": args.spill_after, "registry": str(registry.path) if registry.path else None, "access_log": args.access_log}, sort_keys=True))
        return 0
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("frontdoor requires the 'frontdoor' extra") from exc
    app = create_app(registry, spill_after=args.spill_after, access_log=Path(args.access_log) if args.access_log else None)
    uvicorn.run(app, host=args.host, port=args.port, workers=1)
    return 0


__all__ = ["create_app", "main"]
