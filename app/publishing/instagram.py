"""Instagram Reels through the official Instagram Graph API (Facebook Login for Business).

Checked against developers.facebook.com (Instagram Platform → Content Publishing, Resumable Uploads), 2026-10:
  1. POST graph.facebook.com/{v}/{ig-user-id}/media  media_type=REELS, upload_type=resumable, caption
     → container id
  2. POST rupload.facebook.com/ig-api-upload/{v}/{container-id}  (Authorization: OAuth, offset, file_size)
     — the local file goes straight to Meta, no public URL is needed; an interrupted upload resumes from
     video_status.uploading_phase.bytes_transferred
  3. GET {container-id}?fields=status_code  until FINISHED (IN_PROGRESS / ERROR / EXPIRED / PUBLISHED)
  4. POST {ig-user-id}/media_publish  creation_id → IG media id
Limit: 100 API-published posts per 24 h (GET {ig-user-id}/content_publishing_limit).
Permissions: instagram_basic, instagram_content_publish, pages_read_engagement (+ pages_show_list).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("publishing.instagram")

REQUIRED_PERMISSIONS = ("instagram_basic", "instagram_content_publish", "pages_read_engagement")
AUTH_CODES = {102, 190, 463, 467}
PERMISSION_CODES = {3, 10} | set(range(200, 300))
RATE_CODES = {4, 9, 17, 32, 613, 80001, 80002}
NOT_READY_CODES = {9007}  # "Media ID is not available": the container is still processing


class GraphError(RuntimeError):
    """An error answer of the Graph API (or the network), with what the UI must show."""

    def __init__(self, message: str, http_status: int | None = None, code: int | None = None,
                 subcode: int | None = None, request_id: str | None = None, transient: bool = False):
        super().__init__(message)
        self.http_status, self.code, self.subcode = http_status, code, subcode
        self.request_id, self.is_transient = request_id, transient

    @property
    def kind(self) -> str:
        """auth | permission | rate | not_ready | transient | invalid"""
        if self.code in AUTH_CODES:
            return "auth"
        if self.code in PERMISSION_CODES:
            return "permission"
        if self.code in RATE_CODES:
            return "rate"
        if self.code in NOT_READY_CODES:
            return "not_ready"
        if self.is_transient or self.http_status is None or self.http_status >= 500 or self.code in (1, 2):
            return "transient"
        return "invalid"

    @property
    def code_label(self) -> str | None:
        if self.code is None:
            return None
        return f"{self.code}" + (f"/{self.subcode}" if self.subcode else "")


@dataclass
class ContainerState:
    status_code: str
    detail: str | None = None


class InstagramClient:
    def __init__(self, token: str, account_id: str | None, api_version: str = "v25.0",
                 transport: httpx.BaseTransport | None = None, timeout: float = 60):
        self.token, self.account_id = token, account_id
        self.graph = f"https://graph.facebook.com/{api_version}"
        self.rupload = f"https://rupload.facebook.com/ig-api-upload/{api_version}"
        self.http = httpx.Client(timeout=httpx.Timeout(timeout, read=600), transport=transport)

    def close(self) -> None:
        self.http.close()

    # ---------- transport ----------
    def _hide(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _call(self, method: str, url: str, **kw) -> dict:
        try:
            r = self.http.request(method, url, **kw)
        except httpx.TransportError as exc:
            raise GraphError(self._hide(f"сеть: {exc.__class__.__name__}: {exc}"), transient=True) from None
        request_id = r.headers.get("x-fb-trace-id") or r.headers.get("x-fb-request-id")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
            err = data.get("error", {}) if isinstance(data, dict) else {}
            msg = err.get("error_user_msg") or err.get("message") or r.text[:300] or f"HTTP {r.status_code}"
            raise GraphError(self._hide(msg), r.status_code, err.get("code"), err.get("error_subcode"),
                             err.get("fbtrace_id") or request_id, bool(err.get("is_transient")))
        return data

    def get(self, path: str, **params) -> dict:
        return self._call("GET", f"{self.graph}/{path}", params={**params, "access_token": self.token})

    def post(self, path: str, **data) -> dict:
        return self._call("POST", f"{self.graph}/{path}", data={**data, "access_token": self.token})

    # ---------- account ----------
    def me(self) -> dict:
        return self.get("me", fields="id,name")

    def permissions(self) -> dict[str, str]:
        rows = self.get("me/permissions").get("data", [])
        return {r["permission"]: r.get("status", "") for r in rows}

    def account(self) -> dict:
        return self.get(self.account_id, fields="id,username,name,profile_picture_url,followers_count,media_count")

    def find_accounts(self) -> list[dict]:
        """Instagram professional accounts linked to the Facebook Pages this token can see."""
        pages = self.get("me/accounts", fields="name,instagram_business_account{id,username}").get("data", [])
        return [{"page": p.get("name"), "id": p["instagram_business_account"]["id"],
                 "username": p["instagram_business_account"].get("username")}
                for p in pages if p.get("instagram_business_account")]

    def publishing_limit(self) -> tuple[int, int | None]:
        rows = self.get(f"{self.account_id}/content_publishing_limit", fields="config,quota_usage").get("data", [])
        row = rows[0] if rows else {}
        return int(row.get("quota_usage") or 0), (row.get("config") or {}).get("quota_total")

    def debug_token(self, app_id: str, app_secret: str) -> dict:
        return self._call("GET", f"{self.graph}/debug_token",
                          params={"input_token": self.token, "access_token": f"{app_id}|{app_secret}"}).get("data", {})

    def exchange_token(self, app_id: str, app_secret: str) -> dict:
        """Long-lived user token → a fresh long-lived token (about 60 days)."""
        return self._call("GET", f"{self.graph}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
            "fb_exchange_token": self.token})

    # ---------- publishing ----------
    def create_container(self, caption: str, share_to_feed: bool = True, thumb_offset_ms: int | None = None) -> str:
        data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                "share_to_feed": "true" if share_to_feed else "false"}
        if thumb_offset_ms is not None:
            data["thumb_offset"] = str(thumb_offset_ms)
        return self.post(f"{self.account_id}/media", **data)["id"]

    def uploaded_bytes(self, container_id: str) -> int:
        status = self.get(container_id, fields="video_status").get("video_status") or {}
        return int((status.get("uploading_phase") or {}).get("bytes_transferred") or 0)

    def upload(self, container_id: str, path: Path, offset: int = 0) -> None:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(offset)
            body = fh.read()
        self._call("POST", f"{self.rupload}/{container_id}", content=body, headers={
            "Authorization": f"OAuth {self.token}", "offset": str(offset), "file_size": str(size),
            "Content-Type": "application/octet-stream"})

    def container_state(self, container_id: str) -> ContainerState:
        d = self.get(container_id, fields="status_code,status")
        return ContainerState(d.get("status_code", "IN_PROGRESS"), d.get("status"))

    def wait_finished(self, container_id: str, poll_seconds: float, timeout_seconds: float,
                      sleep=time.sleep) -> ContainerState:
        end = time.monotonic() + timeout_seconds
        while True:
            st = self.container_state(container_id)
            if st.status_code != "IN_PROGRESS" or time.monotonic() >= end:
                return st
            sleep(poll_seconds)

    def publish(self, container_id: str) -> str:
        return self.post(f"{self.account_id}/media_publish", creation_id=container_id)["id"]

    def media(self, media_id: str) -> dict:
        return self.get(media_id, fields="id,permalink,timestamp,media_product_type")

    def recent_media(self, limit: int = 25) -> list[dict]:
        return self.get(f"{self.account_id}/media", fields="id,caption,permalink,timestamp",
                        limit=str(limit)).get("data", [])
