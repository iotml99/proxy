"""
MCP OAuth Proxy - Azure Function
=================================
Fixes AADSTS901002 / AADSTS650057 when connecting Claude to Microsoft MCP servers.

Root cause: Claude sends `resource` parameter in OAuth requests.
Entra ID v2 rejects this entirely (901002) or rejects the value (650057).

This proxy:
- Serves its own OAuth discovery metadata (so Claude uses proxy endpoints)
- Strips `resource` from authorize and token requests
- Formats scope correctly as {resource}/.default
- Forwards all MCP tool calls transparently to the real backend

Claude points at this proxy. Proxy handles auth. Real MCP server never sees bad requests.
"""

import azure.functions as func
import logging
import os
import httpx
import urllib.parse
import json

logger = logging.getLogger(__name__)

# ── Configuration (set these in Azure Function App → Configuration) ──────────
# The real MCP server Claude should ultimately talk to
MCP_BACKEND_URL = os.environ.get(
    "MCP_BACKEND_URL",
    "https://mcp.svc.cloud.microsoft/enterprise",  # default: Graph Enterprise MCP
)

# Entra tenant ID
TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "2c9181be-be3c-411d-9760-5ae4ede5a4af")

# The resource/audience of the backend MCP server
# For Graph Enterprise MCP: api://e8c77dc2-69b3-43f4-bc51-3213c9d915b4
# For Fabric MCP:           https://api.fabric.microsoft.com
MCP_RESOURCE = os.environ.get(
    "MCP_RESOURCE", "api://e8c77dc2-69b3-43f4-bc51-3213c9d915b4"
)

# Entra endpoints
ENTRA_AUTH_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize"
ENTRA_TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"


async def main(req: func.HttpRequest) -> func.HttpResponse:
    path = req.route_params.get("path", "")
    method = req.method.upper()

    logger.info(f"Proxy: {method} /{path}")

    # ── CORS preflight ────────────────────────────────────────────────────────
    if method == "OPTIONS":
        return func.HttpResponse(status_code=204, headers=_cors_headers())

    # ── OAuth discovery metadata ──────────────────────────────────────────────
    if path in (
        ".well-known/oauth-protected-resource",
        ".well-known/oauth-authorization-server",
        ".well-known/openid-configuration",
    ):
        return _handle_metadata(req, path)

    # ── OAuth authorize (redirect) ────────────────────────────────────────────
    if path == "oauth/authorize":
        return _handle_authorize(req)

    # ── OAuth token exchange ──────────────────────────────────────────────────
    if path == "oauth/token":
        return await _handle_token(req)

    # ── Everything else → forward to real MCP backend ────────────────────────
    return await _forward_to_backend(req, path)


# ── OAuth metadata ────────────────────────────────────────────────────────────


def _handle_metadata(req: func.HttpRequest, path: str) -> func.HttpResponse:
    """
    Tell Claude that THIS proxy is the OAuth server.
    Claude reads this and sends all auth requests here instead of to Entra directly.
    """
    base = _proxy_base_url(req)

    if path == ".well-known/oauth-protected-resource":
        # MCP Protected Resource Metadata (RFC 9728)
        # This is what Claude reads first when it connects to an MCP server
        data = {
            "resource": base,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [f"{MCP_RESOURCE}/.default"],
        }
    else:
        # OAuth Authorization Server Metadata
        data = {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
        }

    return func.HttpResponse(
        json.dumps(data),
        status_code=200,
        mimetype="application/json",
        headers=_cors_headers(),
    )


# ── OAuth authorize ───────────────────────────────────────────────────────────


def _handle_authorize(req: func.HttpRequest) -> func.HttpResponse:
    """
    Redirect to real Entra login, but with resource stripped and scope fixed.
    """
    params = dict(req.params)

    logger.info(f"Authorize params BEFORE fix: {list(params.keys())}")

    # THE FIX: remove resource parameter
    params.pop("resource", None)

    # Ensure scope is set correctly
    params["scope"] = _fix_scope(params.get("scope", ""))

    logger.info(f"Authorize params AFTER fix: {params.get('scope')}")

    redirect = f"{ENTRA_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return func.HttpResponse(status_code=302, headers={"Location": redirect})


# ── OAuth token ───────────────────────────────────────────────────────────────


async def _handle_token(req: func.HttpRequest) -> func.HttpResponse:
    """
    Forward token request to Entra with resource stripped and scope fixed.
    This is the critical fix point.
    """
    try:
        body = req.get_body().decode("utf-8")
        params = dict(urllib.parse.parse_qsl(body))

        logger.info(f"Token params BEFORE fix: {list(params.keys())}")

        # THE FIX: remove resource parameter
        params.pop("resource", None)

        # Ensure scope is set correctly
        params["scope"] = _fix_scope(params.get("scope", ""))

        logger.info(f"Token params AFTER fix: {params.get('scope')}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                ENTRA_TOKEN_URL,
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        logger.info(f"Entra token response: {resp.status_code}")

        return func.HttpResponse(
            resp.content,
            status_code=resp.status_code,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    except Exception as e:
        logger.error(f"Token proxy error: {e}")
        return func.HttpResponse(
            json.dumps({"error": "proxy_error", "error_description": str(e)}),
            status_code=500,
            mimetype="application/json",
            headers=_cors_headers(),
        )


# ── MCP traffic forwarding ────────────────────────────────────────────────────


async def _forward_to_backend(req: func.HttpRequest, path: str) -> func.HttpResponse:
    """
    Forward all MCP protocol traffic transparently to the real backend.
    Claude never knows it's talking to a proxy.
    """
    try:
        target_url = f"{MCP_BACKEND_URL.rstrip('/')}/{path}"
        if req.params:
            target_url += f"?{urllib.parse.urlencode(dict(req.params))}"

        # Forward all headers except host
        headers = {
            k: v
            for k, v in req.headers.items()
            if k.lower() not in ("host", "content-length")
        }

        logger.info(f"Forwarding to backend: {req.method} {target_url}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.request(
                method=req.method,
                url=target_url,
                headers=headers,
                content=req.get_body(),
            )

        logger.info(f"Backend response: {resp.status_code}")

        # Build response headers, add CORS
        resp_headers = dict(resp.headers)
        resp_headers.update(_cors_headers())
        # Remove hop-by-hop headers
        for h in ("transfer-encoding", "connection", "keep-alive"):
            resp_headers.pop(h, None)

        return func.HttpResponse(
            body=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
        )

    except Exception as e:
        logger.error(f"Backend forward error: {e}")
        return func.HttpResponse(
            json.dumps({"error": "proxy_error", "error_description": str(e)}),
            status_code=502,
            mimetype="application/json",
            headers=_cors_headers(),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fix_scope(scope: str) -> str:
    """
    Ensure scope contains the correct /.default for the MCP resource.
    Strips any raw resource values, keeps openid/profile/offline_access.
    """
    standard = {"openid", "profile", "email", "offline_access"}
    parts = [s for s in scope.split() if s in standard]

    # Always include the /.default scope for the MCP resource
    default_scope = f"{MCP_RESOURCE}/.default"
    if default_scope not in parts:
        parts.insert(0, default_scope)

    return " ".join(parts)


def _proxy_base_url(req: func.HttpRequest) -> str:
    host = req.headers.get("x-forwarded-host") or req.headers.get("host", "")
    scheme = req.headers.get("x-forwarded-proto", "https")
    return f"{scheme}://{host}/api/proxy"


def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
    }
