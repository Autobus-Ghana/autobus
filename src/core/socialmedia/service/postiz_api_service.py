import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)


class PostizAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _timeout_error(action: str) -> PostizAPIError:
    return PostizAPIError(
        f"Postiz timed out while {action}. Try again in a moment.",
        status_code=504,
    )


def postiz_error_user_message(status_code: int, text: str) -> str:
    """Turn a Postiz error body into a short message we can show or log."""
    raw = (text or "").strip()
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    if isinstance(data, dict):
        msg = data.get("message") or data.get("msg") or data.get("error")
        if isinstance(msg, list):
            msg = "; ".join(str(x) for x in msg if x is not None)
        name = data.get("name") or data.get("provider")
        if msg:
            label = str(name).strip() if name else ""
            text_msg = str(msg).strip()
            if label and text_msg:
                return f"{label}: {text_msg}"
            return text_msg or label
    if raw:
        snippet = raw if len(raw) <= 400 else raw[:400] + "…"
        return f"Postiz create post failed ({status_code}): {snippet}"
    return f"Postiz create post failed ({status_code})"


def normalize_postiz_integrations_list(payload: Any) -> List[Dict[str, Any]]:
    """
    Postiz Public API documents `GET /integrations` as a JSON array.
    Some builds or intermediaries return the same rows under a wrapper key.
    """
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in (
            "integrations",
            "items",
            "data",
            "value",
            "results",
            "channels",
        ):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def _normalize_postiz_email(email: str) -> str:
    """
    Postiz lowercases email on register but not on login; DB lookup is exact match.
    """
    return (email or "").strip().lower()


def _extract_session_jwt(res: httpx.Response) -> Optional[str]:
    """
    Postiz attaches the session JWT to the `auth` response header when NOT_SECURED is set,
    and/or to Set-Cookie. Autobus calls Postiz by Docker hostname (e.g. postiz:5000) while
    cookies may be scoped to the public domain — forward JWT explicitly on /api/user/self.
    """
    auth = res.headers.get("auth")
    if auth:
        return auth.strip()
    raw = getattr(res.headers, "raw", None) or []
    for key, value in raw:
        if key.lower() != b"set-cookie":
            continue
        text = value.decode("latin-1", errors="replace")
        m = re.search(r"(?i)\bauth=([^;]+)", text)
        if m:
            return unquote(m.group(1).strip().strip('"'))
    return None


def _auth_request_headers(jwt: Optional[str]) -> Dict[str, str]:
    if not jwt:
        return {}
    return {"auth": jwt}


def normalize_postiz_company(company: str, *, fallback: str = "Autobus Client") -> str:
    """
    Postiz `CreateOrgUserDto` requires company length 3–128 (class-validator).
    Autobus user fields can be shorter (e.g. two-letter brand); use a longer fallback.
    """
    name = (company or "").strip()
    if len(name) >= 3:
        return name[:128]
    fb = (fallback or "Autobus Client").strip()
    if len(fb) >= 3:
        return fb[:128]
    return "Org"[:128]


class PostizClient:
    """
    Minimal Postiz client for:
    - provisioning: POST /api/auth/register then GET /api/user/self
    - publishing: POST /api/public/v1/posts

    Notes:
    - For self-hosted Postiz, the public API base is `{POSTIZ_BASE_URL}/api/public/v1`.
    - The `/api/user/self` endpoint returns `orgId` and (for admins) `publicApi` (org API key).
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def provision_org_and_get_public_api_key(
        self,
        email: str,
        company: str,
        password: str,
        timeout_s: float = 20.0,
    ) -> Tuple[str, str]:
        """
        Creates a Postiz org + SUPERADMIN user via `/api/auth/register`,
        then calls `/api/user/self` to obtain:
        - organization id
        - organization public API key (used for `/api/public/v1/*`)
        """
        company_norm = normalize_postiz_company(company)
        email_norm = _normalize_postiz_email(email)
        session_jwt: Optional[str] = None
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
        ) as client:
            reg = await client.post(
                self._url("/api/auth/register"),
                json={
                    "provider": "LOCAL",
                    "email": email_norm,
                    "password": password,
                    "company": company_norm,
                },
            )
            # Postiz returns 400 with plain-text body for business errors (see auth.controller catch).
            # Duplicate email is 400 "Email already exists", not 409 — still recoverable via login.
            reg_ok = reg.status_code < 400
            duplicate_email = (
                reg.status_code == 400
                and "email already exists" in (reg.text or "").lower()
            )
            if not reg_ok and reg.status_code != 409 and not duplicate_email:
                raise PostizAPIError(
                    f"Postiz register failed ({reg.status_code}): {reg.text}"
                )

            if reg_ok:
                session_jwt = _extract_session_jwt(reg) or session_jwt

            me = await client.get(
                self._url("/api/user/self"),
                headers=_auth_request_headers(session_jwt),
            )
            if me.status_code == 401:
                # Some Postiz builds do not establish an authenticated session on register.
                login = await client.post(
                    self._url("/api/auth/login"),
                    json={
                        "provider": "LOCAL",
                        "email": email_norm,
                        "password": password,
                        "providerToken": "",
                    },
                )
                if login.status_code >= 400:
                    raise PostizAPIError(
                        f"Postiz login failed ({login.status_code}): {login.text}"
                    )
                session_jwt = _extract_session_jwt(login) or session_jwt
                me = await client.get(
                    self._url("/api/user/self"),
                    headers=_auth_request_headers(session_jwt),
                )

            if me.status_code >= 400:
                raise PostizAPIError(f"Postiz self failed ({me.status_code}): {me.text}")
            data = me.json()
            org_id = data.get("orgId") or data.get("organizationId") or data.get("id")
            public_api_key = data.get("publicApi") or data.get("apiKey")

            if not org_id or not public_api_key:
                raise PostizAPIError(
                    "Postiz self response missing orgId/publicApi; ensure registration succeeded and user has admin role."
                )

            return str(org_id), str(public_api_key)

    async def login_local(
        self,
        email: str,
        password: str,
        timeout_s: float = 20.0,
    ) -> Dict[str, Any]:
        """
        Login against Postiz LOCAL auth provider.
        Returns the response body, and raises for HTTP errors.
        """
        email_norm = _normalize_postiz_email(email)
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
        ) as client:
            res = await client.post(
                self._url("/api/auth/login"),
                json={
                    "provider": "LOCAL",
                    "email": email_norm,
                    "password": password,
                    "providerToken": "",
                },
            )
            if res.status_code >= 400:
                raise PostizAPIError(
                    f"Postiz login failed ({res.status_code}): {res.text}"
                )

            if not res.text.strip():
                return {}
            return res.json()

    async def create_post(
        self,
        public_api_key: str,
        payload: Dict[str, Any],
        timeout_s: float = 20.0,
    ) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.post(
                self._url("/api/public/v1/posts"),
                headers={"Authorization": public_api_key, "Content-Type": "application/json"},
                json=payload,
            )
            if res.status_code >= 400:
                logger.error(
                    "Postiz create post failed (%s): %s",
                    res.status_code,
                    res.text,
                )
                raise PostizAPIError(
                    postiz_error_user_message(res.status_code, res.text),
                    status_code=res.status_code,
                    body=res.text or "",
                )
            return res.json()

    async def list_integrations(
        self,
        public_api_key: str,
        timeout_s: float = 8.0,
    ) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                res = await client.get(
                    self._url("/api/public/v1/integrations"),
                    headers={"Authorization": public_api_key},
                )
        except httpx.TimeoutException as exc:
            raise _timeout_error("listing connected channels") from exc
        if res.status_code >= 400:
            raise PostizAPIError(
                f"Postiz list integrations failed ({res.status_code}): {res.text}"
            )
        return res.json()

    async def get_social_connect_url(
        self,
        public_api_key: str,
        integration: str,
        *,
        refresh: Optional[str] = None,
        timeout_s: float = 12.0,
    ) -> str:
        """
        OAuth URL for connecting a channel via Postiz Public API
        (`GET /api/public/v1/social/{integration}`).
        """
        slug = (integration or "").strip().lower()
        if not slug:
            raise PostizAPIError("integration slug is required")

        params: Dict[str, str] = {}
        if refresh:
            params["refresh"] = refresh.strip()

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                res = await client.get(
                    self._url(f"/api/public/v1/social/{slug}"),
                    headers={"Authorization": public_api_key},
                    params=params or None,
                )
        except httpx.TimeoutException as exc:
            raise _timeout_error(f"starting {slug} connect") from exc
        if res.status_code >= 400:
            raise PostizAPIError(
                f"Postiz social connect failed ({res.status_code}): {res.text}"
            )
        data = res.json() if res.text.strip() else {}
        if isinstance(data, dict):
            url = data.get("url") or data.get("authorization_url")
            if url:
                return str(url).strip()
        raise PostizAPIError(
            "Postiz social connect response missing url; "
            "ensure the provider client id/secret are configured on Postiz "
            f"(integration={slug})."
        )

    async def delete_integration(
        self,
        public_api_key: str,
        integration_id: str,
        timeout_s: float = 20.0,
    ) -> Dict[str, Any]:
        """
        Disconnect a channel via Postiz Public API
        (`DELETE /api/public/v1/integrations/{id}`).
        """
        iid = (integration_id or "").strip()
        if not iid:
            raise PostizAPIError("integration id is required")

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.delete(
                self._url(f"/api/public/v1/integrations/{iid}"),
                headers={"Authorization": public_api_key},
            )
            # Postiz treats missing channels as already deleted.
            if res.status_code == 404:
                return {"id": iid, "deleted": True, "already_gone": True}
            if res.status_code >= 400:
                raise PostizAPIError(
                    f"Postiz delete integration failed ({res.status_code}): {res.text}"
                )
            if not res.text.strip():
                return {"id": iid, "deleted": True}
            data = res.json()
            if isinstance(data, dict):
                return data
            return {"id": iid, "deleted": True, "value": data}

    async def login_local_session(
        self,
        email: str,
        password: str,
        timeout_s: float = 20.0,
    ) -> str:
        """LOCAL login that returns the Postiz session JWT (``auth`` header/cookie)."""
        email_norm = _normalize_postiz_email(email)
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
        ) as client:
            res = await client.post(
                self._url("/api/auth/login"),
                json={
                    "provider": "LOCAL",
                    "email": email_norm,
                    "password": password,
                    "providerToken": "",
                },
            )
            if res.status_code >= 400:
                raise PostizAPIError(
                    f"Postiz login failed ({res.status_code}): {res.text}"
                )
            jwt = _extract_session_jwt(res)
            if not jwt:
                raise PostizAPIError("Postiz login did not return a session token")
            return jwt

    async def call_integration_function(
        self,
        session_jwt: str,
        *,
        integration_id: str,
        name: str,
        data: Optional[Dict[str, Any]] = None,
        timeout_s: float = 20.0,
    ) -> Any:
        """Call Postiz ``POST /api/integrations/function`` (uses the stored provider token)."""
        iid = (integration_id or "").strip()
        method = (name or "").strip()
        if not iid or not method:
            raise PostizAPIError("integration id and function name are required")
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.post(
                self._url("/api/integrations/function"),
                headers={
                    **_auth_request_headers(session_jwt),
                    "Content-Type": "application/json",
                },
                json={"id": iid, "name": method, "data": data or {}},
            )
        if res.status_code >= 400:
            raise PostizAPIError(
                f"Postiz integration function failed ({res.status_code}): {res.text}",
                status_code=res.status_code,
                body=res.text or "",
            )
        if not res.text.strip():
            return {}
        try:
            return res.json()
        except Exception:
            return {"raw": res.text}

    async def get_tiktok_creator_info_public(
        self,
        public_api_key: str,
        integration_id: str,
        timeout_s: float = 20.0,
    ) -> Dict[str, Any]:
        """Optional Postiz public API: ``GET /integrations/{id}/tiktok/creator-info``."""
        iid = (integration_id or "").strip()
        if not iid:
            raise PostizAPIError("integration id is required")
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.get(
                self._url(f"/api/public/v1/integrations/{iid}/tiktok/creator-info"),
                headers={"Authorization": public_api_key},
            )
        if res.status_code >= 400:
            raise PostizAPIError(
                f"Postiz TikTok creator-info failed ({res.status_code}): {res.text}",
                status_code=res.status_code,
                body=res.text or "",
            )
        return res.json() if res.text.strip() else {}

    async def list_posts(
        self,
        public_api_key: str,
        *,
        start_date: str,
        end_date: str,
        timeout_s: float = 20.0,
    ) -> Any:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.get(
                self._url("/api/public/v1/posts"),
                headers={"Authorization": public_api_key},
                params={"startDate": start_date, "endDate": end_date},
            )
        if res.status_code >= 400:
            raise PostizAPIError(
                f"Postiz list posts failed ({res.status_code}): {res.text}",
                status_code=res.status_code,
                body=res.text or "",
            )
        if not res.text.strip():
            return []
        return res.json()


def facebook_login_config_id() -> str:
    """Login for Business config for Postiz Page publishing (not WhatsApp ES)."""
    return (
        os.getenv("FACEBOOK_LOGIN_CONFIG_ID", "").strip()
        or os.getenv("POSTIZ_FACEBOOK_CONFIG_ID", "").strip()
        or os.getenv("META_FACEBOOK_PAGE_CONFIG_ID", "").strip()
    )


def apply_facebook_login_config_id(
    authorization_url: str,
    *,
    config_id: Optional[str] = None,
    slug: Optional[str] = None,
) -> str:
    """
    Rewrite Postiz's Facebook OAuth URL for Facebook Login for Business.

    Postiz only sends ``scope=...``. Business-type Meta apps need ``config_id``
    instead. Mixing leftover ``scope`` / ``auth_type`` with ``config_id``, or
    omitting ``response_type=code`` + ``override_default_response_type=true``,
    makes Meta show "Sorry, something went wrong" on the Continue-as screen.
    """
    url = (authorization_url or "").strip()
    if not url:
        return url
    if slug and slug.strip().lower() != "facebook":
        return url
    cid = (config_id or facebook_login_config_id()).strip()
    if not cid:
        return url

    parts = urlsplit(url)
    host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"facebook.com", "m.facebook.com", "web.facebook.com"}:
        return url

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["config_id"] = cid
    # Login for Business: config_id replaces scope. Do not keep auth_type or
    # WhatsApp Embedded Signup extras on a Page-publishing dialog.
    for drop in ("scope", "auth_type", "extras"):
        query.pop(drop, None)
    query["response_type"] = "code"
    query["override_default_response_type"] = "true"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def oauth_host(authorization_url: str) -> str:
    """Hostname of an OAuth URL, without a leading www."""
    parts = urlsplit((authorization_url or "").strip())
    host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def require_direct_provider_url(authorization_url: str, slug: str) -> str:
    """TikTok must open TikTok. YouTube must open Google. Never Zernio."""
    url = (authorization_url or "").strip()
    host = oauth_host(url)
    provider = (slug or "").strip().lower()
    if host == "zernio.com" or host.endswith(".zernio.com"):
        label = "TikTok" if provider == "tiktok" else "YouTube"
        raise PostizAPIError(
            f"{label} has to open its own sign-in. This link pointed at Zernio.",
            status_code=502,
        )
    allowed = {
        "tiktok": {"tiktok.com"},
        "youtube": {"accounts.google.com", "accounts.youtube.com"},
    }.get(provider, set())
    if allowed and host not in allowed:
        label = "TikTok" if provider == "tiktok" else "Google"
        raise PostizAPIError(
            f"{label} sign-in did not start. Try linking again.",
            status_code=502,
        )
    return url


# Postiz hardcodes this on the TikTok authorize URL. TikTok no longer offers it
# as an addable scope, so the whole login fails with "correct the following: scope".
_TIKTOK_DROPPED_SCOPES = frozenset({"video.list", "video.create"})


def apply_tiktok_oauth_scopes(
    authorization_url: str,
    *,
    slug: Optional[str] = None,
) -> str:
    """Rewrite TikTok authorize URLs before we send the user to Login Kit.

    Drop deprecated scopes Postiz still puts on the URL, and always set
    ``disable_auto_auth=1``. Without that flag TikTok skips login and
    consent when the same phone already authorized this client.
    """
    url = (authorization_url or "").strip()
    if not url:
        return url
    if slug and slug.strip().lower() != "tiktok":
        return url

    parts = urlsplit(url)
    host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"tiktok.com", "www.tiktok.com"}:
        return url

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    raw_scope = query.get("scope") or ""
    if raw_scope:
        kept = [
            item.strip()
            for item in raw_scope.replace(" ", ",").split(",")
            if item.strip() and item.strip() not in _TIKTOK_DROPPED_SCOPES
        ]
        if kept:
            query["scope"] = ",".join(kept)

    # TikTok Login Kit: 0 (default) auto-approves a prior grant; 1 shows
    # the authorization page so the user can log in and accept terms again.
    query["disable_auto_auth"] = "1"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def coerce_tiktok_privacy_for_unaudited_app(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Unaudited TikTok apps reject Direct Post with PUBLIC_TO_EVERYONE
    (``unaudited_client_can_only_post_to_private_accounts``). Postiz still
    returns 200 because publish runs later in Temporal.
    Set TIKTOK_ALLOW_PUBLIC_POST=true after TikTok audits public posting.
    """
    if os.getenv("TIKTOK_ALLOW_PUBLIC_POST", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return payload
    posts = payload.get("posts")
    if not isinstance(posts, list):
        return payload
    for post in posts:
        if not isinstance(post, dict):
            continue
        settings = post.get("settings")
        if not isinstance(settings, dict):
            continue
        if str(settings.get("__type") or "").strip().lower() != "tiktok":
            continue
        privacy = str(settings.get("privacy_level") or "").strip()
        if privacy in {"", "SELF_ONLY"}:
            continue
        logger.warning(
            "[SOCIAL] TikTok privacy %s is not allowed until the app is "
            "audited for public Direct Post; using SELF_ONLY",
            privacy,
        )
        settings["privacy_level"] = "SELF_ONLY"
    return payload


def postiz_enabled() -> bool:
    return bool(os.getenv("POSTIZ_BASE_URL", "").strip())


def generate_postiz_password(length: int = 28) -> str:
    # Strong random password; not stored (Postiz cookies/api key used instead).
    return uuid.uuid4().hex + uuid.uuid4().hex[: max(0, length - 32)]


def derive_postiz_password(*, username: str) -> str:
    """
    Postiz LOCAL sign-in password: Autobus username (``fullname``), not the Autobus
    login password. Email is used as the Postiz account identifier.
    """
    from utilities.integration_credentials import integration_local_password

    return integration_local_password(username=username)

