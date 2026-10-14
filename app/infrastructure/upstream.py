from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import httpx

from app.domain.auth import Auth
from app.domain.persistence import AccountPersistence
from app.config.settings import Settings


UPDATE_API = "https://api-overmind.youdao.com/openapi/get/luna/hardware/lobsterai/prod/update"


class UpstreamError(Exception):
    def __init__(self, kind: str, status: int, message: str):
        super().__init__(f"upstream {kind} (http {status}): {message[:200]}")
        self.kind, self.status = kind, status


def classify(status: int, body: str) -> str:
    text = body.lower()
    if status == 402 or any(x in text for x in ("insufficient credit", "quota exceeded", "credit exhausted", "积分不足", "额度不足", "余额不足")):
        return "hard_credit"
    if any(x in body for x in ("40100", "40101", "token rejected", "refresh token was rejected")):
        return "session_dead"
    if status == 429:
        return "soft_rate"
    if status == 404:
        return "not_found"
    if status >= 500:
        return "server"
    return "client"


class Upstream:
    def __init__(self, settings: Settings, store: AccountPersistence):
        """
        创建上游客户端。

        Args:
            settings: 运行时配置。
            store: 刷新令牌后用于写回账号凭证的存储。
        """
        if not settings.upstream_base:
            raise RuntimeError("upstream base URL is empty")
        self.base = settings.upstream_base.rstrip("/")
        self.store = store
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10,
                read=settings.timeout,
                write=30,
                pool=10,
            ),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            trust_env=False,
        )
        self._client_version: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    def headers(self, auth: Auth) -> dict[str, str]:
        return {"Authorization": f"Bearer {auth.access_token}", "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json", "User-Agent": "LobsterAI/0.1.0",
                "X-LobsterAI-Client-Capabilities": "kimi-k3-agentic-v1", "X-LobsterAI-Client-Version": "0.1.0"}

    async def api_request(self, auth: Auth, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send an authenticated upstream request after ensuring the token is valid."""
        if auth.needs_refresh:
            await self.refresh(auth)
        headers = self.headers(auth)
        headers.update(kwargs.pop("headers", {}))
        return await self.client.request(method, self.base + path, headers=headers, **kwargs)

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...] | None:
        match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:-[0-9A-Za-z.-]+)?", version.strip())
        return tuple(int(part) for part in match.group(1).split(".")) if match else None

    async def client_version(self) -> str:
        """Resolve and cache the official desktop client version for activities."""
        if self._client_version:
            return self._client_version
        response = await self.client.get(UPDATE_API, timeout=15, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise UpstreamError("client_version", response.status_code, f"GET {UPDATE_API}: {response.text}")
        try:
            version = str(response.json()["data"]["value"]["version"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"official update API returned invalid version: {exc}") from exc
        if not self._version_key(version):
            raise RuntimeError(f"official update API returned invalid version: {version!r}")
        self._client_version = version
        return version

    async def _activity_request(
        self,
        auth: Auth,
        method: str,
        path: str,
        *,
        client_version: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if client_version:
            headers.update({
                "User-Agent": f"LobsterAI/{client_version}",
                "X-LobsterAI-Client-Version": client_version,
            })
        response = await self.api_request(
            auth,
            method,
            path,
            headers=headers,
            **kwargs,
        )
        if response.status_code >= 400:
            raise UpstreamError(classify(response.status_code, response.text), response.status_code, f"{method} {self.base + path}: {response.text}")
        try:
            envelope = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"activity API returned invalid JSON from {method} {self.base + path}: {exc}") from exc
        if envelope.get("code") != 0:
            message = envelope.get("message") or envelope.get("msg") or "activity API failed"
            raise UpstreamError("activity", response.status_code, f"code={envelope.get('code')} msg={message}")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("activity API data is empty; accessToken may be invalid")
        return data

    async def exchange(self, auth_code: str, uuid_value: str, first_keyfrom: str) -> dict[str, Any]:
        """Exchange a browser login code for account credentials."""
        body = {
            "authCode": auth_code,
            "firstKeyfrom": first_keyfrom,
            "latestKeyfrom": str(int(time.time() * 1000)),
            "uuid": uuid_value,
            "version": "0.1.0",
        }
        response = await self.client.post(
            self.base + "/api/auth/exchange",
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "LobsterAI/0.1.0"},
        )
        if response.status_code >= 400:
            raise UpstreamError(classify(response.status_code, response.text), response.status_code, f"POST {self.base}/api/auth/exchange: {response.text}")
        try:
            envelope = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"login exchange returned invalid JSON: {exc}") from exc
        if envelope.get("code") != 0:
            message = envelope.get("message") or envelope.get("msg") or "login exchange failed"
            raise UpstreamError("login_exchange", response.status_code, message)
        data = envelope.get("data")
        if not isinstance(data, dict) or not data.get("accessToken"):
            raise RuntimeError("login exchange returned no accessToken")
        return data

    async def refresh(self, auth: Auth) -> None:
        if not auth.refresh_token:
            raise UpstreamError("session_dead", 401, "no refreshToken")
        body = auth.keyfrom() | {"refreshToken": auth.refresh_token}
        response = await self.client.post(self.base + "/api/auth/refresh", json=body, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise UpstreamError(classify(response.status_code, response.text), response.status_code, f"POST {self.base}/api/auth/refresh: {response.text}")
        envelope = response.json()
        data = envelope.get("data", envelope)
        token = data.get("accessToken", "")
        if not token:
            raise UpstreamError("session_dead", response.status_code, "refresh response has no accessToken")
        auth.access_token, auth.refresh_token = token, data.get("refreshToken", auth.refresh_token)
        auth.expires_at = int(time.time()) + int(data.get("expiresIn", 3600))
        await self.store.save_auth(auth)

    async def chat(self, auth: Auth, body: bytes) -> httpx.Response:
        payload = json.loads(body)
        payload["stream"] = True
        if payload.get("tool_choice") in (None, "", "none"):
            payload.pop("tool_choice", None)
        response = await self.api_request(auth, "POST", "/api/proxy/v1/chat/completions", json=payload)
        if response.status_code >= 400:
            raise UpstreamError(classify(response.status_code, response.text), response.status_code, f"POST {self.base}/api/proxy/v1/chat/completions: {response.text}")
        return response

    @asynccontextmanager
    async def chat_stream(self, auth: Auth, body: bytes) -> AsyncIterator[httpx.Response]:
        """Open a real streaming chat response and close it after consumption."""
        if auth.needs_refresh:
            await self.refresh(auth)
        payload = json.loads(body)
        payload["stream"] = True
        if payload.get("tool_choice") in (None, "", "none"):
            payload.pop("tool_choice", None)
        headers = self.headers(auth)
        request = self.client.build_request(
            "POST",
            self.base + "/api/proxy/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        response = await self.client.send(request, stream=True)
        if response.status_code >= 400:
            try:
                body_text = (await response.aread()).decode(errors="replace")
            finally:
                await response.aclose()
            raise UpstreamError(
                classify(response.status_code, body_text),
                response.status_code,
                f"POST {self.base}/api/proxy/v1/chat/completions: {body_text}",
            )
        try:
            yield response
        finally:
            await response.aclose()

    async def models(self, auth: Auth) -> list[str]:
        response = await self.api_request(auth, "GET", "/api/models/available")
        if response.status_code != 200:
            return []
        return [item.get("modelId") for item in response.json().get("data", []) if item.get("modelId")]

    async def checkin(self, auth: Auth) -> dict[str, Any]:
        """Claim today's available LobsterAI activity reward."""
        version = await self.client_version()
        query = urlencode({
            "placement": "desktop_sidebar",
            "clientVersion": version,
            "containerApiVersion": "2",
            "platform": "win32",
        })
        slot = await self._activity_request(
            auth,
            "GET",
            f"/api/client-activities/slot?{query}",
            client_version=version,
        )
        if slot.get("slotState") != "available" or not slot.get("activity"):
            return {"checked_in": False, "message": f"无可用活动（slotState={slot.get('slotState')!r}）"}

        activity = slot["activity"]
        activity_code = activity["activityCode"]
        revision = activity["configRevision"]
        context = await self._activity_request(
            auth,
            "GET",
            f"/api/client-activities/{activity_code}/context?{urlencode({'configRevision': revision})}",
            client_version=version,
        )
        if context.get("state", {}).get("claimedToday") or "check_in" not in (context.get("actions") or []):
            return {"checked_in": False, "message": "今天已签到，跳过"}

        result = await self._activity_request(
            auth,
            "POST",
            f"/api/client-activities/{activity_code}/actions/check_in",
            client_version=version,
            json={
                "configRevision": revision,
                "idempotencyKey": str(uuid.uuid4()),
                "payload": {},
            },
        )
        reward = result.get("result") or {}
        gained = next(
            (reward[key] for key in ("creditsGranted", "rewardCredits", "credits")
             if isinstance(reward.get(key), (int, float))),
            None,
        )
        return {"checked_in": True, "message": "签到成功", "credits_gained": gained}

    async def quota(self, auth: Auth) -> int | None:
        response = await self.api_request(auth, "GET", "/api/user/profile-summary")
        if response.status_code >= 400:
            return None
        envelope = response.json()
        data = envelope.get("data", envelope)
        value = data.get("totalCreditsRemaining")
        return max(0, int(value)) if value is not None else None
