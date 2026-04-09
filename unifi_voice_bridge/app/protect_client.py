from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from settings import AppSettings


class ProtectClientError(Exception):
    pass


class ProtectClient:
    def __init__(self, settings: AppSettings, logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger.getChild("protect")
        self._session: Optional[aiohttp.ClientSession] = None
        self._csrf_token: Optional[str] = None
        self._bootstrap_cache: Optional[Dict[str, Any]] = None
        self._camera_map_cache: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return f"https://{self._settings.addon.protect_host}:{self._settings.addon.protect_port}"

    async def start(self) -> None:
        async with self._lock:
            if self._session is not None and not self._session.closed:
                return
            timeout = aiohttp.ClientTimeout(total=30)
            cookie_jar = aiohttp.CookieJar(unsafe=True)
            connector = aiohttp.TCPConnector(ssl=self._build_ssl_context())
            self._session = aiohttp.ClientSession(timeout=timeout, cookie_jar=cookie_jar, connector=connector)
        await self.login()
        await self.get_bootstrap(force=True)

    async def close(self) -> None:
        async with self._lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = None
            self._csrf_token = None
            self._bootstrap_cache = None
            self._camera_map_cache = {}

    async def login(self, force: bool = False) -> None:
        session = await self._ensure_session()
        if self._csrf_token and not force:
            return
        url = f"{self.base_url}/api/auth/login"
        payload = {"username": self._settings.addon.protect_username, "password": self._settings.addon.protect_password}
        self._logger.info("Logging in to UniFi Protect at %s", self.base_url)
        async with session.post(url, json=payload) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                raise ProtectClientError(f"Protect login failed: {response.status} {response.reason} - {text[:500]}")
            self._update_csrf_token(response)
            self._logger.info("Protect login successful.")

    async def ping(self) -> bool:
        try:
            await self.get_bootstrap(force=False)
            return True
        except Exception as exc:
            self._logger.warning("Protect ping failed: %s", exc)
            return False

    async def get_bootstrap(self, force: bool = False) -> Dict[str, Any]:
        if self._bootstrap_cache is not None and not force:
            return self._bootstrap_cache
        data = await self._request_json("GET", "/proxy/protect/api/bootstrap")
        if not isinstance(data, dict):
            raise ProtectClientError("Unexpected bootstrap response. Expected JSON object.")
        self._bootstrap_cache = data
        self._camera_map_cache = self._extract_camera_map_from_bootstrap(data)
        self._logger.info("Loaded Protect bootstrap. cameras=%s", len(self._camera_map_cache))
        return data

    async def get_camera_map(self, force: bool = False) -> Dict[str, str]:
        if self._camera_map_cache and not force:
            return dict(self._camera_map_cache)
        await self.get_bootstrap(force=force)
        return dict(self._camera_map_cache)

    async def get_recent_events(self, camera_id: Optional[str] = None, seconds_back: int = 20, event_types: Optional[List[str]] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if seconds_back < 1:
            seconds_back = 1
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(seconds=seconds_back)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        path = f"/proxy/protect/api/events?start={start_ms}&end={end_ms}"
        raw = await self._request_json("GET", path)
        events = self._normalize_events_response(raw)
        if camera_id:
            events = [event for event in events if self._extract_camera_id(event) == camera_id]
        if event_types:
            wanted = {str(x).strip() for x in event_types if str(x).strip()}
            events = [event for event in events if self._extract_event_type(event) in wanted]
        events.sort(key=self._extract_event_timestamp_ms, reverse=True)
        if limit is not None and limit >= 0:
            events = events[:limit]
        return events

    async def get_camera_audio_stream_url(self, camera_id: str, *, prefer_secure: bool = False) -> Optional[str]:
        bootstrap = await self.get_bootstrap(force=False)
        camera = self._find_camera_bootstrap(bootstrap, camera_id)
        if not camera:
            return None
        channels = camera.get("channels")
        if not isinstance(channels, list):
            channels = []
        ranked = sorted(channels, key=self._channel_rank)
        for channel in ranked:
            if not isinstance(channel, dict):
                continue
            direct = self._extract_direct_stream_url(channel, prefer_secure=prefer_secure)
            if direct:
                return direct
        for channel in ranked:
            if not isinstance(channel, dict):
                continue
            alias = self._extract_stream_alias(channel, prefer_secure=prefer_secure)
            if alias:
                return self._build_stream_url_from_alias(alias, prefer_secure=prefer_secure)
        return None

    async def _request_json(self, method: str, path: str, *, allow_retry_on_401: bool = True) -> Any:
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        headers = self._build_headers()
        async with session.request(method, url, headers=headers) as response:
            if response.status == 401 and allow_retry_on_401:
                self._logger.warning("Protect request returned 401. Re-authenticating.")
                await self.login(force=True)
                return await self._request_json(method, path, allow_retry_on_401=False)
            if response.status < 200 or response.status >= 300:
                body = await response.text()
                raise ProtectClientError(f"Protect request failed: {method} {path} -> {response.status} {response.reason} - {body[:500]}")
            self._update_csrf_token(response)
            try:
                return await response.json(content_type=None)
            except Exception as exc:
                body = await response.text()
                raise ProtectClientError(f"Failed to decode JSON from Protect for {method} {path}: {exc}. Body preview: {body[:500]}") from exc

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            await self.start()
        if self._session is None or self._session.closed:
            raise ProtectClientError("Protect session is not available.")
        return self._session

    def _build_ssl_context(self) -> ssl.SSLContext | bool:
        if not self._settings.addon.protect_allow_insecure_tls:
            return True
        self._logger.warning("Protect TLS certificate validation is disabled.")
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _build_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._csrf_token:
            headers["X-CSRF-Token"] = self._csrf_token
        return headers

    def _update_csrf_token(self, response: aiohttp.ClientResponse) -> None:
        token = response.headers.get("X-CSRF-Token")
        if token:
            self._csrf_token = token

    def _normalize_events_response(self, raw: Any) -> List[Dict[str, Any]]:
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(raw, dict):
            data = raw.get("data")
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        return []

    def _extract_camera_map_from_bootstrap(self, payload: Dict[str, Any]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        cameras = payload.get("cameras")
        if not isinstance(cameras, list):
            return result
        for item in cameras:
            if not isinstance(item, dict):
                continue
            camera_id = self._to_str(item.get("id"))
            name = self._to_str(item.get("name")) or camera_id
            if camera_id:
                result[camera_id] = name
        return result

    def _extract_event_type(self, payload: Dict[str, Any]) -> str:
        return self._to_str(payload.get("type"))

    def _extract_event_timestamp_ms(self, payload: Dict[str, Any]) -> int:
        try:
            return int(payload.get("start"))
        except Exception:
            return 0

    def _extract_camera_id(self, payload: Dict[str, Any]) -> Optional[str]:
        camera = payload.get("camera")
        if isinstance(camera, str):
            return camera.strip() or None
        if isinstance(camera, dict):
            for key in ("id", "_id", "cameraId"):
                value = self._to_str(camera.get(key))
                if value:
                    return value
        for key in ("cameraId", "camera_id", "deviceId", "device_id"):
            value = self._to_str(payload.get(key))
            if value:
                return value
        return None

    def _find_camera_bootstrap(self, bootstrap: Dict[str, Any], camera_id: str) -> Optional[Dict[str, Any]]:
        cameras = bootstrap.get("cameras")
        if not isinstance(cameras, list):
            return None
        for item in cameras:
            if not isinstance(item, dict):
                continue
            if self._to_str(item.get("id")) == camera_id:
                return item
        return None

    def _channel_rank(self, channel: Dict[str, Any]) -> tuple[int, int]:
        name = self._to_str(channel.get("name")).casefold()
        try:
            width = int(channel.get("width") or 0)
        except Exception:
            width = 0
        try:
            height = int(channel.get("height") or 0)
        except Exception:
            height = 0
        if "high" in name:
            priority = 0
        elif "medium" in name:
            priority = 1
        elif "low" in name:
            priority = 2
        else:
            priority = 3
        return (priority, -(width * height))

    def _extract_direct_stream_url(self, channel: Dict[str, Any], *, prefer_secure: bool) -> Optional[str]:
        keys = ("rtspsUrl", "rtspsURL", "rtsps", "url") if prefer_secure else ("rtspUrl", "rtspURL", "rtsp", "url")
        for key in keys:
            value = self._to_str(channel.get(key))
            if value.startswith("rtsp://") or value.startswith("rtsps://"):
                return value
        return None

    def _extract_stream_alias(self, channel: Dict[str, Any], *, prefer_secure: bool) -> Optional[str]:
        keys = ("rtspsAlias", "rtsps_alias", "alias") if prefer_secure else ("rtspAlias", "rtsp_alias", "alias")
        for key in keys:
            value = self._to_str(channel.get(key))
            if value:
                return value
        return None

    def _build_stream_url_from_alias(self, alias: str, *, prefer_secure: bool) -> str:
        alias = alias.strip()
        if alias.startswith("rtsp://") or alias.startswith("rtsps://"):
            return alias if prefer_secure else self._convert_rtsps_to_rtsp(alias)
        if prefer_secure:
            return f"rtsps://{self._settings.addon.protect_host}:7441/{alias}"
        return f"rtsp://{self._settings.addon.protect_host}:7447/{alias.split('?', 1)[0]}"

    def _convert_rtsps_to_rtsp(self, value: str) -> str:
        text = value.strip()
        if text.startswith("rtsps://"):
            text = "rtsp://" + text[len("rtsps://"):]
        text = text.replace(":7441/", ":7447/")
        if "?" in text:
            text = text.split("?", 1)[0]
        return text

    def _to_str(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()
