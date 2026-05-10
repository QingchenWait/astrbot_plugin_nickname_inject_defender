from __future__ import annotations

import re
from collections.abc import Iterable
from sys import maxsize
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


MODE_INHERIT = "继承自系统"
MODE_MANUAL = "手动设置"


class NicknameInjectDefenderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=maxsize)
    async def guard_nickname_wake_word(self, event: AstrMessageEvent) -> None:
        """阻止仅由用户昵称、引用昵称或 @ 昵称中的唤醒词触发的群聊消息。"""
        wake_words = self._get_wake_words()
        if not wake_words:
            return

        nickname_text = await self._collect_nickname_text(event)
        if not nickname_text:
            return

        nickname_wake_words = self._find_wake_words(nickname_text, wake_words)
        if not nickname_wake_words:
            return

        user_message_text = self._extract_user_message_text(event)
        if self._find_wake_words(user_message_text, wake_words):
            return

        wake_word = nickname_wake_words[0]
        logger.info(
            f"由于唤醒词 {wake_word} 仅在用户昵称中存在，终止消息传递",
        )
        event.stop_event()

    def _get_wake_words(self) -> list[str]:
        mode = str(self.config.get("wake_words_mode", MODE_INHERIT)).strip()
        if mode == MODE_MANUAL:
            return self._normalize_wake_words(self.config.get("manual_wake_words", []))

        inherited = self._get_inherited_wake_words()
        if inherited:
            return inherited

        return self._normalize_wake_words(self.config.get("manual_wake_words", []))

    def _get_inherited_wake_words(self) -> list[str]:
        config_manager = getattr(self.context, "astrbot_config_mgr", None)
        confs = getattr(config_manager, "confs", None)

        wake_words: list[str] = []
        if isinstance(confs, dict):
            for conf in confs.values():
                wake_prefix = self._safe_get(conf, "wake_prefix", [])
                wake_words.extend(self._normalize_wake_words(wake_prefix))
            return self._deduplicate(wake_words)

        get_config = getattr(self.context, "get_config", None)
        if callable(get_config):
            try:
                conf = get_config()
            except Exception:
                return []
            return self._normalize_wake_words(self._safe_get(conf, "wake_prefix", []))

        return []

    @staticmethod
    def _safe_get(mapping: Any, key: str, default: Any) -> Any:
        get_value = getattr(mapping, "get", None)
        if callable(get_value):
            return get_value(key, default)
        return getattr(mapping, key, default)

    @staticmethod
    def _normalize_wake_words(raw_wake_words: Any) -> list[str]:
        if raw_wake_words is None:
            return []
        if isinstance(raw_wake_words, str):
            candidates: Iterable[Any] = [raw_wake_words]
        elif isinstance(raw_wake_words, Iterable):
            candidates = raw_wake_words
        else:
            candidates = [raw_wake_words]

        wake_words: list[str] = []
        for candidate in candidates:
            if candidate is None:
                continue
            wake_word = str(candidate).strip()
            if wake_word:
                wake_words.append(wake_word)
        return NicknameInjectDefenderPlugin._deduplicate(wake_words)

    @staticmethod
    def _deduplicate(items: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @staticmethod
    def _find_wake_words(text: str, wake_words: list[str]) -> list[str]:
        if not text:
            return []
        return [
            wake_word
            for wake_word in sorted(wake_words, key=len, reverse=True)
            if wake_word in text
        ]

    async def _collect_nickname_text(self, event: AstrMessageEvent) -> str:
        names: list[str] = []
        self._append_name(names, event.get_sender_name())

        self_id = str(event.get_self_id())
        group_id = str(event.get_group_id())
        bot = getattr(event, "bot", None)
        for component in event.get_messages():
            component_type = str(getattr(component, "type", "")).lower()
            if component_type == "at":
                qq = str(getattr(component, "qq", ""))
                if qq and qq not in {self_id, "all"}:
                    name = getattr(component, "name", "")
                    if not name:
                        name = await self._get_group_member_name(bot, group_id, qq)
                    self._append_name(names, name)
            elif component_type == "reply":
                sender_id = str(getattr(component, "sender_id", ""))
                if sender_id and sender_id == self_id:
                    continue

                name = getattr(component, "sender_nickname", "")
                if not name:
                    reply_id = str(getattr(component, "id", ""))
                    sender_id, name = await self._get_reply_sender(bot, reply_id)
                    if sender_id and sender_id == self_id:
                        continue
                self._append_name(names, name)

        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        await self._collect_raw_nicknames(raw_message, names, self_id, group_id, bot)
        return "\n".join(self._deduplicate(names))

    async def _collect_raw_nicknames(
        self,
        raw_message: Any,
        names: list[str],
        self_id: str,
        group_id: str,
        bot: Any,
    ) -> None:
        if raw_message is None:
            return

        sender = self._raw_get(raw_message, "sender")
        if isinstance(sender, dict):
            self._append_name(names, sender.get("card") or sender.get("nickname"))

        message_segments = self._iter_raw_message_segments(
            self._raw_get(raw_message, "message"),
        )
        if not message_segments:
            return

        for segment in message_segments:
            segment_type, data = self._get_segment_type_and_data(segment)
            if not isinstance(data, dict):
                continue
            if segment_type == "at":
                qq = str(data.get("qq", ""))
                if qq and qq not in {self_id, "all"}:
                    name = data.get("name") or data.get("card") or data.get("nickname")
                    if not name:
                        name = await self._get_group_member_name(bot, group_id, qq)
                    self._append_name(names, name)
            elif segment_type == "reply":
                sender_id = str(data.get("sender_id") or data.get("user_id") or "")
                if sender_id and sender_id == self_id:
                    continue

                name = (
                    data.get("sender_nickname")
                    or data.get("nickname")
                    or data.get("name")
                )
                if not name:
                    reply_id = str(data.get("id", ""))
                    sender_id, name = await self._get_reply_sender(bot, reply_id)
                    if sender_id and sender_id == self_id:
                        continue
                self._append_name(names, name)

    async def _get_group_member_name(
        self,
        bot: Any,
        group_id: str,
        user_id: str,
    ) -> str:
        if not bot or not group_id or not user_id or user_id == "all":
            return ""

        try:
            member = await bot.call_action(
                action="get_group_member_info",
                group_id=self._onebot_id(group_id),
                user_id=self._onebot_id(user_id),
                no_cache=False,
            )
        except Exception:
            member = None

        if isinstance(member, dict):
            name = member.get("card") or member.get("nickname") or member.get("nick")
            if name:
                return str(name)

        try:
            stranger = await bot.call_action(
                action="get_stranger_info",
                user_id=self._onebot_id(user_id),
                no_cache=False,
            )
        except Exception:
            return ""

        if not isinstance(stranger, dict):
            return ""
        return str(stranger.get("nick") or stranger.get("nickname") or "")

    async def _get_reply_sender(self, bot: Any, reply_id: str) -> tuple[str, str]:
        if not bot or not reply_id:
            return "", ""

        try:
            reply_message = await bot.call_action(
                action="get_msg",
                message_id=self._onebot_id(reply_id),
            )
        except Exception:
            return "", ""

        if not isinstance(reply_message, dict):
            return "", ""
        sender = reply_message.get("sender") or {}
        if not isinstance(sender, dict):
            return "", ""

        sender_id = str(sender.get("user_id") or "")
        name = sender.get("card") or sender.get("nickname") or sender.get("nick") or ""
        return sender_id, str(name)

    @staticmethod
    def _onebot_id(value: str) -> int | str:
        return int(value) if value.isdigit() else value

    @staticmethod
    def _append_name(names: list[str], name: Any) -> None:
        if name is None:
            return
        text = str(name).strip()
        if text:
            names.append(text)

    def _extract_user_message_text(self, event: AstrMessageEvent) -> str:
        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        raw_text = self._extract_raw_onebot_text(raw_message)
        if raw_text is not None:
            return raw_text

        text_parts: list[str] = []
        for component in event.get_messages():
            component_type = str(getattr(component, "type", "")).lower()
            if component_type in {"plain", "text"} and hasattr(component, "text"):
                text_parts.append(str(getattr(component, "text", "")))
        return "".join(text_parts).strip()

    def _extract_raw_onebot_text(self, raw_message: Any) -> str | None:
        if raw_message is None:
            return None

        message = self._raw_get(raw_message, "message")
        if isinstance(message, str):
            return re.sub(r"\[CQ:[^\]]+\]", "", message).strip()

        message_segments = self._iter_raw_message_segments(message)
        if message_segments:
            text_parts: list[str] = []
            for segment in message_segments:
                segment_type, data = self._get_segment_type_and_data(segment)
                if not isinstance(data, dict):
                    continue
                if segment_type in {"text", "plain"}:
                    text_parts.append(str(data.get("text", "")))
                elif segment_type == "markdown":
                    text_parts.append(
                        str(data.get("markdown") or data.get("content") or ""),
                    )
            return "".join(text_parts).strip()

        raw_text = self._raw_get(raw_message, "raw_message")
        if isinstance(raw_text, str):
            return re.sub(r"\[CQ:[^\]]+\]", "", raw_text).strip()

        return None

    @staticmethod
    def _iter_raw_message_segments(message: Any) -> list[Any]:
        if message is None or isinstance(message, (str, bytes)):
            return []
        if isinstance(message, dict):
            return [message]
        try:
            return list(message)
        except TypeError:
            return []

    def _get_segment_type_and_data(self, segment: Any) -> tuple[str, Any]:
        if isinstance(segment, dict):
            return str(segment.get("type", "")).lower(), segment.get("data") or {}
        return str(self._raw_get(segment, "type") or "").lower(), (
            self._raw_get(segment, "data") or {}
        )

    @staticmethod
    def _raw_get(raw_message: Any, key: str) -> Any:
        if isinstance(raw_message, dict):
            return raw_message.get(key)
        try:
            return raw_message[key]
        except Exception:
            return getattr(raw_message, key, None)

    async def terminate(self) -> None:
        pass
