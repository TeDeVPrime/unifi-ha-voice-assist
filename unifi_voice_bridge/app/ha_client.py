from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp


class HomeAssistantClientError(Exception):
    pass


@dataclass(frozen=True)
class ConversationResult:
    conversation_id: Optional[str]
    continue_conversation: bool
    response_type: Optional[str]
    response_language: Optional[str]
    response_speech: Optional[str]
    success_targets: List[str] = field(default_factory=list)
    failed_targets: List[str] = field(default_factory=list)
    raw_response: Dict[str, Any] = field(default_factory=dict)


class HomeAssistantClient:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger.getChild("ha")
        self._session: Optional[aiohttp.ClientSession] = None
        self._base_url = "http://supervisor/core/api"
        self._token = os.environ.get("SUPERVISOR_TOKEN", "").strip()

    async def start(self) -> None:
        if not self._token:
            raise HomeAssistantClientError("SUPERVISOR_TOKEN is missing. Make sure the add-on has homeassistant_api: true.")
        if self._session is not None and not self._session.closed:
            return
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(timeout=timeout, headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": "application/json"})
        self._logger.info("Home Assistant Core API client started.")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def ping(self) -> bool:
        try:
            data = await self.get_config()
            return isinstance(data, dict)
        except Exception as exc:
            self._logger.warning("Home Assistant ping failed: %s", exc)
            return False

    async def get_config(self) -> Dict[str, Any]:
        return await self._request_json("GET", "/config")

    async def process_conversation(self, text: str, *, language: Optional[str] = None, agent_id: Optional[str] = None, conversation_id: Optional[str] = None) -> ConversationResult:
        sentence = (text or "").strip()
        if not sentence:
            raise HomeAssistantClientError("Conversation text cannot be empty.")
        payload: Dict[str, Any] = {"text": sentence}
        if language:
            payload["language"] = language
        if agent_id:
            payload["agent_id"] = agent_id
        if conversation_id:
            payload["conversation_id"] = conversation_id
        data = await self._request_json("POST", "/conversation/process", json=payload)
        if not isinstance(data, dict):
            raise HomeAssistantClientError("Unexpected response from conversation.process")
        response = data.get("response")
        if not isinstance(response, dict):
            response = {}
        response_data = response.get("data")
        if not isinstance(response_data, dict):
            response_data = {}
        result = ConversationResult(
            conversation_id=_to_optional_str(data.get("conversation_id")),
            continue_conversation=bool(data.get("continue_conversation", False)),
            response_type=_to_optional_str(response.get("response_type")),
            response_language=_to_optional_str(response.get("language")),
            response_speech=self._extract_response_speech(response),
            success_targets=self._extract_target_ids(response_data.get("success")),
            failed_targets=self._extract_target_ids(response_data.get("failed")),
            raw_response=data,
        )
        self._logger.info("Conversation processed. response_type=%s success=%s failed=%s", result.response_type or "", ",".join(result.success_targets) if result.success_targets else "", ",".join(result.failed_targets) if result.failed_targets else "")
        return result

    async def speak(self, *, tts_entity: str, media_player_entity_id: str, message: str, language: Optional[str] = None, cache: bool = False, options: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        tts_entity = (tts_entity or "").strip()
        media_player_entity_id = (media_player_entity_id or "").strip()
        message = (message or "").strip()
        if not tts_entity:
            raise HomeAssistantClientError("tts_entity is required for speak().")
        if not media_player_entity_id:
            raise HomeAssistantClientError("media_player_entity_id is required for speak().")
        if not message:
            raise HomeAssistantClientError("message is required for speak().")
        payload: Dict[str, Any] = {"target": {"entity_id": tts_entity}, "data": {"media_player_entity_id": media_player_entity_id, "message": message, "cache": cache}}
        if language:
            payload["data"]["language"] = language
        if options:
            payload["data"]["options"] = options
        data = await self.call_service("tts", "speak", payload)
        self._logger.info("TTS speak called. tts_entity=%s media_player=%s message_length=%s", tts_entity, media_player_entity_id, len(message))
        return data

    async def call_service(self, domain: str, service: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        domain = (domain or "").strip()
        service = (service or "").strip()
        if not domain or not service:
            raise HomeAssistantClientError("Both domain and service are required.")
        data = await self._request_json("POST", f"/services/{domain}/{service}", json=payload)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []

    async def _request_json(self, method: str, path: str, *, json: Optional[Dict[str, Any]] = None) -> Any:
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        async with session.request(method, url, json=json) as response:
            if response.status < 200 or response.status >= 300:
                body = await response.text()
                raise HomeAssistantClientError(f"Home Assistant API request failed: {method} {path} -> {response.status} {response.reason} - {body[:500]}")
            if response.content_type == "application/json":
                return await response.json(content_type=None)
            text = await response.text()
            if not text.strip():
                return {}
            try:
                return await response.json(content_type=None)
            except Exception:
                return {"raw_text": text}

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            await self.start()
        if self._session is None or self._session.closed:
            raise HomeAssistantClientError("Home Assistant session is not available.")
        return self._session

    def _extract_response_speech(self, response: Dict[str, Any]) -> Optional[str]:
        speech = response.get("speech")
        if not isinstance(speech, dict):
            return None
        plain = speech.get("plain")
        if not isinstance(plain, dict):
            return None
        return _to_optional_str(plain.get("speech"))

    def _extract_target_ids(self, items: Any) -> List[str]:
        result: List[str] = []
        if not isinstance(items, list):
            return result
        for item in items:
            if not isinstance(item, dict):
                continue
            value = _to_optional_str(item.get("id"))
            if value:
                result.append(value)
        return result


def _to_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
