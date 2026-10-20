"""AstrBot: QQ group intake and delayed send via local companion."""
from __future__ import annotations
import asyncio
import json
import logging
from urllib.request import Request, urlopen
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api.message_components import At

LOGGER = logging.getLogger(__name__)

@register("qq_gpt_companion", "Local", "QQ GPT companion with local memory/handoff", "0.1.0")
class QQGPTCompanion(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = config
        self._worker_task = None

    def _base(self):
        return str(self.settings.get("local_bridge_url", "http://127.0.0.1:18761")).rstrip("/")

    def _token(self):
        return str(self.settings.get("local_token", ""))

    def _call(self, path: str, body=None):
        if not self._token():
            raise RuntimeError("local_token not configured")
        data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
        req = Request(self._base() + path, data=data,
                      headers={"X-Companion-Token": self._token(), "Content-Type": "application/json"},
                      method="POST")
        with urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._poll_outbound())
            LOGGER.info("QQ GPT companion outbound watcher started")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        # Privacy gate: do not transmit or store group messages until explicitly enabled.
        if not bool(self.settings.get("allow_message_intake", False)):
            return
        obj = event.message_obj
        group_id = str(getattr(obj, "group_id", "") or "")
        allow = {str(v) for v in self.settings.get("allowed_group_ids", [])}
        if not group_id or group_id not in allow:
            return
        message_id = str(getattr(obj, "message_id", "") or "")
        sender = str(event.get_sender_id())
        self_id = str(getattr(obj, "self_id", "") or "")
        if not message_id or not sender or sender == self_id:
            return
        text = str(getattr(obj, "message_str", "") or "")
        if not text.strip():
            return
        mentioned = any(isinstance(seg, At) and str(getattr(seg, "qq", "")) == self_id
                        for seg in getattr(obj, "message", []))
        direct = mentioned or text.strip().lower().startswith(("gpt ", "gpt，", "gpt:", "gpt："))
        payload = {
            "group_id": group_id, "message_id": message_id, "user_id": sender,
            "user_name": str(event.get_sender_name() or "群友"), "text": text,
            "origin": str(event.unified_msg_origin), "directed": direct,
            "timestamp": str(getattr(obj, "timestamp", "") or "")
        }
        try:
            result = await asyncio.to_thread(self._call, "/api/events", payload)
        except Exception as exc:
            LOGGER.warning("Local companion intake unavailable: %s", type(exc).__name__)
            return
        if result.get("accepted", False):
            event.stop_event()

    def _onebot_ready(self) -> bool:
        """Wait for OneBot's reverse API websocket before claiming a queued send."""
        platform = self.context.get_platform_inst("qq-gpt-companion")
        bot = getattr(platform, "bot", None)
        return bool(getattr(bot, "_wsr_api_clients", None))

    async def _poll_outbound(self):
        while True:
            if not bool(self.settings.get("allow_outbound_sends", False)):
                await asyncio.sleep(4)
                continue
            if not self._onebot_ready():
                await asyncio.sleep(1)
                continue
            try:
                result = await asyncio.to_thread(self._call, "/api/outbound/claim", {})
                item = result.get("item")
                if item:
                    req_id = item["id"]
                    try:
                        delivered = await self.context.send_message(
                            item["origin"], MessageChain().message(item["reply_text"])
                        )
                        if delivered is not True:
                            raise RuntimeError("AstrBot outbound platform not found")
                    except Exception as exc:
                        details = f"{type(exc).__name__}: {exc}"
                        LOGGER.warning("QQ GPT companion outbound uncertain: %s", details[:150])
                        await asyncio.to_thread(
                            self._call, "/api/outbound/ack",
                            {"request_id": req_id, "outcome": "uncertain", "details": details[:150]}
                        )
                    else:
                        await asyncio.to_thread(
                            self._call, "/api/outbound/ack",
                            {"request_id": req_id, "outcome": "sent"}
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.debug("Companion outbound poll paused: %s", type(exc).__name__)
            await asyncio.sleep(1)

    async def terminate(self):
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
