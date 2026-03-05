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

    logger.info("=" * 60)
    logger.info(f"REQUEST  {method} /{path}")
    logger.info(
        f"From:    {req.headers.get('x-forwarded-for', req.headers.get('client-ip', 'unknown'))}"
    )
    logger.info(f"UA:      {req.headers.get('user-agent', '-')[:120]}")
    if req.params:
        logger.info(f"Params:  {dict(req.params)}")

    # ── CORS preflight ────────────────────────────────────────────────────────
    if method == "OPTIONS":
        logger.info("RESPONSE 204 CORS preflight")
        return func.HttpResponse(status_code=204, headers=_cors_headers())

    # ── OAuth discovery metadata ──────────────────────────────────────────────
    if path in (
        ".well-known/oauth-protected-resource",
        ".well-known/oauth-authorization-server",
        ".well-known/openid-configuration",
    ):
        logger.info(f"HANDLER  metadata → {path}")
        resp = _handle_metadata(req, path)
        logger.info(f"RESPONSE {resp.status_code} {path}")
        return resp

    # ── OAuth authorize (redirect) ────────────────────────────────────────────
    if path == "oauth/authorize":
        logger.info("HANDLER  authorize")
        resp = _handle_authorize(req)
        logger.info(f"RESPONSE {resp.status_code} redirect → Entra")
        return resp

    # ── OAuth token exchange ──────────────────────────────────────────────────
    if path == "oauth/token":
        logger.info("HANDLER  token exchange")
        resp = await _handle_token(req)
        logger.info(f"RESPONSE {resp.status_code} token exchange complete")
        return resp

    # ── Everything else → forward to real MCP backend ────────────────────────
    logger.info(f"HANDLER  forward → backend /{path}")
    resp = await _forward_to_backend(req, path)
    logger.info(f"RESPONSE {resp.status_code} forwarded")
    return resp


# ── OAuth metadata ────────────────────────────────────────────────────────────


def _handle_metadata(req: func.HttpRequest, path: str) -> func.HttpResponse:
    """
    Tell Claude that THIS proxy is the OAuth server.
    Claude reads this and sends all auth requests here instead of to Entra directly.
    """
    base = _proxy_base_url(req)
    logger.info(f"Metadata base URL: {base}")

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

    had_resource = "resource" in params
    logger.info(f"Authorize  keys in: {list(params.keys())}")
    logger.info(
        f"Authorize  resource present: {had_resource}  value: {params.get('resource', '-')}"
    )
    logger.info(f"Authorize  scope in: {params.get('scope', '-')}")

    # THE FIX: remove resource parameter
    params.pop("resource", None)

    # Ensure scope is set correctly
    params["scope"] = _fix_scope(params.get("scope", ""))

    logger.info(f"Authorize  scope out: {params.get('scope')}")
    logger.info(f"Authorize  client_id: {params.get('client_id', '-')}")
    logger.info(f"Authorize  redirect_uri: {params.get('redirect_uri', '-')}")

    redirect = f"{ENTRA_AUTH_URL}?{urllib.parse.urlencode(params)}"
    logger.info(f"Authorize  → {ENTRA_AUTH_URL}")
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

        had_resource = "resource" in params
        logger.info(f"Token  keys in: {list(params.keys())}")
        logger.info(f"Token  grant_type: {params.get('grant_type', '-')}")
        logger.info(
            f"Token  resource present: {had_resource}  value: {params.get('resource', '-')}"
        )
        logger.info(f"Token  scope in: {params.get('scope', '-')}")
        logger.info(f"Token  client_id: {params.get('client_id', '-')}")

        # THE FIX: remove resource parameter
        params.pop("resource", None)

        # Ensure scope is set correctly
        params["scope"] = _fix_scope(params.get("scope", ""))

        logger.info(f"Token  scope out: {params.get('scope')}")
        logger.info(f"Token  → POST {ENTRA_TOKEN_URL}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                ENTRA_TOKEN_URL,
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        logger.info(f"Token  Entra responded: {resp.status_code}")
        if resp.status_code != 200:
            logger.warning(f"Token  Entra error body: {resp.text[:500]}")

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

        auth_header = headers.get("authorization", "")
        logger.info(f"Forward  {req.method} {target_url}")
        logger.info(
            f"Forward  auth header present: {bool(auth_header)}  type: {auth_header.split()[0] if auth_header else '-'}"
        )
        logger.info(f"Forward  content-type: {headers.get('content-type', '-')}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.request(
                method=req.method,
                url=target_url,
                headers=headers,
                content=req.get_body(),
            )

        logger.info(f"Forward  backend responded: {resp.status_code}")
        if resp.status_code >= 400:
            logger.warning(f"Forward  backend error body: {resp.text[:500]}")

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
    return f"{scheme}://{host}"


def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
    }
