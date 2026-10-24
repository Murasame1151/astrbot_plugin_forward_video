"""引用合并转发消息，提取其中的视频并逐条发送。

仅支持 OneBot v11（AstrBot 的 aiocqhttp 适配器 / NapCat、Lagrange 等）。

整体流程：
1. 从入站原始事件里找到 reply（引用）段的 id；
2. 调用 get_msg 拿到被引用消息，从中找到 forward 段的 id；
3. 调用 get_forward_msg 拿到节点列表；
4. 递归遍历节点（含内联的嵌套合并转发），收集所有视频 URL；
5. 按配置的间隔逐条发送。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Video
from astrbot.api.star import Context, Star

# ---------------------------------------------------------------------------
# 常量 / 正则
# ---------------------------------------------------------------------------

_HTTP_PREFIXES = ("http://", "https://")

# CQ 码里的转义字符，先还原再做 URL 判断
_CQ_UNESCAPE = (
    ("&#44;", ","),
    ("&#91;", "["),
    ("&#93;", "]"),
    ("&amp;", "&"),
)

_CQ_VIDEO_RE = re.compile(r"\[CQ:video,([^\]]*)\]")
_CQ_FORWARD_RE = re.compile(r"\[CQ:forward,[^\]]*?\bid=([^,\]]+)")
_CQ_REPLY_RE = re.compile(r"\[CQ:reply,[^\]]*?\bid=([^,\]]+)")
_CQ_PARAM_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)=([^,\]]*)")

_FORWARD_TYPES = {"forward", "node", "nodes"}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _unescape_cq(value: str) -> str:
    for old, new in _CQ_UNESCAPE:
        value = value.replace(old, new)
    return value


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _component_type_name(segment: Any) -> str:
    """统一取消息段类型名，兼容 dict 和 AstrBot 的 ComponentType 枚举。"""
    if isinstance(segment, dict):
        value = segment.get("type")
    else:
        value = getattr(segment, "type", None)
    # ComponentType 是 str Enum，str(ComponentType.Reply) 会得到 "ComponentType.Reply"，
    # 所以优先取 .value。
    value = getattr(value, "value", value)
    return _as_str(value).lower()


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(_HTTP_PREFIXES)


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unwrap_payload(payload: Any) -> Any:
    """兼容 aiocqhttp.call_action 直接返回 data 或返回完整响应体两种情况。"""
    if not isinstance(payload, dict):
        return payload
    inner = payload.get("data")
    if not isinstance(inner, (dict, list)):
        return payload
    # 完整响应体通常带 status/retcode；也可能是仅有 data 的包装
    if "status" in payload or "retcode" in payload or set(payload) <= {"data"}:
        return inner
    return payload


# ---------------------------------------------------------------------------
# 入站事件解析
# ---------------------------------------------------------------------------


def extract_reply_id(raw_event: Any) -> str | None:
    """从 OneBot 事件中取出被引用消息的 id。

    优先读取 message 数组里的 reply 段；读不到时再解析 raw_message 中的 CQ 码。
    """
    if raw_event is None:
        return None

    message = None
    try:
        if isinstance(raw_event, dict):
            message = raw_event.get("message")
        else:
            message = raw_event.get("message")
    except Exception:  # pragma: no cover - 防御非 mapping 对象
        message = None

    if message is None:
        message = getattr(raw_event, "message", None)

    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict):
                # aiocqhttp 也可能给出 Segment 对象
                segment = getattr(segment, "__dict__", None)
                if not isinstance(segment, dict):
                    continue
            if _as_str(segment.get("type")).lower() != "reply":
                continue
            data = segment.get("data") or {}
            if not isinstance(data, dict):
                continue
            reply_id = data.get("id") or data.get("message_id")
            if _as_str(reply_id):
                return _as_str(reply_id)

    raw_message = None
    try:
        if isinstance(raw_event, dict):
            raw_message = raw_event.get("raw_message")
        else:
            raw_message = raw_event.get("raw_message")
    except Exception:  # pragma: no cover
        raw_message = None
    if raw_message is None:
        raw_message = getattr(raw_event, "raw_message", None)

    if isinstance(raw_message, str):
        match = _CQ_REPLY_RE.search(raw_message)
        if match:
            return _unescape_cq(match.group(1)).strip()
    return None


def _reference_from_segment(segment: Any) -> tuple[str | None, Any | None]:
    """从一个消息段里找合并转发：返回 (forward_id, 内联 content)。"""
    if not isinstance(segment, dict):
        return None, None
    if _component_type_name(segment) not in _FORWARD_TYPES:
        return None, None

    data = segment.get("data") or {}
    if not isinstance(data, dict):
        return None, None

    content = data.get("content")
    # NapCat 在 get_forward_msg 中会把嵌套转发内联展开在 data.content
    if isinstance(content, (list, tuple, dict)) and len(content) > 0:
        return None, content

    forward_id = data.get("id") or data.get("message_id")
    if _as_str(forward_id):
        return _as_str(forward_id), None

    if isinstance(content, (list, tuple, dict)):
        return None, content
    return None, None


def _forward_id_from_raw(payload: dict[str, Any]) -> str | None:
    """从 NapCat 的 raw 字段里兜底提取转发 id。

    合并转发消息的 raw.parentMsgIdList 在部分版本里会保存 forward id，
    但普通消息也可能有 parentMsgIdList，因此只在 raw 明确是合并转发时使用。
    """
    raw = payload.get("raw")
    if not isinstance(raw, dict):
        return None

    elements = raw.get("elements")
    has_forward_element = False
    if isinstance(elements, list):
        for element in elements:
            if isinstance(element, dict) and element.get("multiForwardMsgElement"):
                has_forward_element = True
                break

    if not has_forward_element and _to_int(raw.get("msgType"), -1) != 8:
        return None

    parents = raw.get("parentMsgIdList")
    if isinstance(parents, list):
        for parent in parents:
            if _as_str(parent):
                return _as_str(parent)
    return None


def find_forward_reference(payload: Any) -> tuple[str | None, Any | None]:
    """从 get_msg 的返回里找到合并转发的 id 或内联内容。"""
    payload = _unwrap_payload(payload)
    if payload is None:
        return None, None

    if isinstance(payload, list):
        for item in payload:
            forward_id, content = _reference_from_segment(item)
            if forward_id or content is not None:
                return forward_id, content
        return None, None

    if not isinstance(payload, dict):
        return None, None

    # payload 本身就是一个 forward 段
    forward_id, content = _reference_from_segment(payload)
    if forward_id or content is not None:
        return forward_id, content

    message = payload.get("message")
    if isinstance(message, list):
        for segment in message:
            forward_id, content = _reference_from_segment(segment)
            if forward_id or content is not None:
                return forward_id, content
    elif isinstance(message, str):
        match = _CQ_FORWARD_RE.search(message)
        if match:
            return _unescape_cq(match.group(1)).strip(), None

    raw_message = payload.get("raw_message")
    if isinstance(raw_message, str):
        match = _CQ_FORWARD_RE.search(raw_message)
        if match:
            return _unescape_cq(match.group(1)).strip(), None

    raw_forward_id = _forward_id_from_raw(payload)
    if raw_forward_id:
        return raw_forward_id, None

    return None, None


def find_forward_id_in_parsed_message(message_obj: Any) -> str | None:
    """从 AstrBot 已解析的 Reply.chain 里直接取 forward id。

    AstrBot 的 aiocqhttp 适配器收到引用消息时会先调 get_msg，并把被引用消息
    解析成 Reply(chain=[...])。如果被引用的是合并转发，chain 里会有
    Forward(id=...)。优先用它可以少调一次 get_msg。
    """
    segments = getattr(message_obj, "message", None)
    if not isinstance(segments, (list, tuple)):
        return None

    for segment in segments:
        if _component_type_name(segment) != "reply":
            continue
        chain = getattr(segment, "chain", None)
        if not isinstance(chain, (list, tuple)):
            continue
        for child in chain:
            if _component_type_name(child) not in _FORWARD_TYPES:
                continue
            forward_id = _as_str(getattr(child, "id", None))
            if forward_id:
                return forward_id
    return None


def extract_messages(payload: Any) -> list[Any]:
    """把 get_forward_msg 的返回统一成节点列表。"""
    payload = _unwrap_payload(payload)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            return messages
        if isinstance(messages, dict):
            # 兼容 content = {"messages": [...]} 的包装
            nested = messages.get("messages")
            if isinstance(nested, list):
                return nested
            return [messages]
        if "message" in payload:
            return [payload]
        content = payload.get("content")
        if isinstance(content, list):
            return content
    return []


# ---------------------------------------------------------------------------
# 视频收集
# ---------------------------------------------------------------------------


@dataclass
class CollectionStats:
    """一次遍历的统计信息，用于日志和失败提示。"""

    urls: list[str] = field(default_factory=list)
    visited: int = 0
    skipped_videos: int = 0
    fetch_errors: int = 0
    node_limited: bool = False
    depth_limited: bool = False
    stop_requested: bool = False


class VideoCollector:
    """递归遍历合并转发节点，按顺序收集视频 URL。

    - 不下载视频，只保留 http(s) URL；
    - 不去重，同一条视频出现几次就收集几次；
    - 支持 data.content 内联的嵌套转发，也支持只有 forward id 时再调一次
      get_forward_msg；
    - 通过 max_videos / max_depth / max_nodes 限制资源消耗。
    """

    def __init__(
        self,
        call_api: Callable[..., Awaitable[Any]],
        max_videos: int = 0,
        max_depth: int = 2,
        max_nodes: int = 500,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._call_api = call_api
        self._should_stop = should_stop
        self.max_videos = max(0, _to_int(max_videos, 0))
        self.max_depth = max(0, _to_int(max_depth, 2))
        self.max_nodes = max(0, _to_int(max_nodes, 500))
        self.stats = CollectionStats()
        self._visited_forward_ids: set[str] = set()

    @property
    def full(self) -> bool:
        return self.max_videos > 0 and len(self.stats.urls) >= self.max_videos

    def _tick(self) -> bool:
        """节点计数；返回 False 表示应当停止遍历。"""
        if self._should_stop is not None:
            try:
                if self._should_stop():
                    self.stats.stop_requested = True
                    return False
            except Exception:
                pass
        if self.full:
            return False
        if self.max_nodes > 0 and self.stats.visited >= self.max_nodes:
            self.stats.node_limited = True
            return False
        self.stats.visited += 1
        return True

    async def walk_messages(self, messages: Any, depth: int = 0) -> None:
        """遍历 get_forward_msg 返回的节点列表。"""
        items = messages
        if isinstance(items, dict):
            if isinstance(items.get("messages"), list):
                items = items["messages"]
            else:
                items = [items]
        elif isinstance(items, str):
            items = [items]

        if not isinstance(items, (list, tuple)):
            return

        for node in items:
            if self.full:
                return
            if not self._tick():
                return
            await self._walk_node(node, depth)

    async def _walk_node(self, node: Any, depth: int) -> None:
        if isinstance(node, str):
            self._collect_from_cq(node)
            return
        if not isinstance(node, dict):
            return

        node_type = _as_str(node.get("type")).lower()
        if node_type == "video":
            self._add_video(node.get("data") or {})
            return
        if node_type in _FORWARD_TYPES:
            await self._walk_forward(node.get("data") or {}, depth)
            return

        # get_forward_msg 的普通节点：{"sender": ..., "message": [...]}
        if "message" in node:
            await self.walk_segments(node.get("message"), depth)
            return
        if "content" in node:
            await self.walk_segments(node.get("content"), depth)
            return

        # 少数实现会把消息再包一层 data
        data = node.get("data")
        if isinstance(data, dict):
            if _as_str(data.get("type")).lower() == "video":
                self._add_video(data)
                return
            if isinstance(data.get("content"), (list, tuple, dict)):
                await self._walk_forward(data, depth)
                return
            if "message" in data:
                await self.walk_segments(data["message"], depth)

    async def walk_segments(self, segments: Any, depth: int = 0) -> None:
        """遍历一个节点内的消息段列表。"""
        if isinstance(segments, str):
            self._collect_from_cq(segments)
            return
        if isinstance(segments, dict):
            segments = [segments]
        if not isinstance(segments, (list, tuple)):
            return

        for segment in segments:
            if self.full:
                return
            if not self._tick():
                return
            if isinstance(segment, str):
                self._collect_from_cq(segment)
                continue
            if not isinstance(segment, dict):
                continue

            segment_type = _as_str(segment.get("type")).lower()
            if segment_type == "video":
                self._add_video(segment.get("data") or {})
            elif segment_type in _FORWARD_TYPES:
                await self._walk_forward(segment.get("data") or {}, depth)
            elif segment_type in {"message"}:
                await self.walk_segments(
                    segment.get("message") or segment.get("content"), depth
                )

    async def _walk_forward(self, data: Any, depth: int) -> None:
        if not isinstance(data, dict):
            return

        content = data.get("content")
        if content is None:
            content = data.get("message")

        has_content = (
            isinstance(content, (list, tuple, dict)) and len(content) > 0
        )
        if has_content:
            if depth >= self.max_depth:
                self.stats.depth_limited = True
                return
            await self.walk_messages(content, depth + 1)
            return

        forward_id = _as_str(data.get("id") or data.get("message_id"))
        if not forward_id:
            return
        if depth >= self.max_depth:
            self.stats.depth_limited = True
            return
        if forward_id in self._visited_forward_ids:
            return

        self._visited_forward_ids.add(forward_id)
        try:
            response = await self._call_api("get_forward_msg", id=forward_id)
        except Exception as exc:
            self.stats.fetch_errors += 1
            logger.warning(
                "[合并转发视频] 获取嵌套合并转发 %s 失败: %s", forward_id, exc
            )
            return

        await self.walk_messages(extract_messages(response), depth + 1)

    def _add_video(self, data: Any) -> None:
        if self.full:
            return
        if not isinstance(data, dict):
            data = {}

        url = ""
        for key in ("url", "file", "path"):
            value = data.get(key)
            if _is_http_url(value):
                url = _unescape_cq(value.strip())
                break

        if not url:
            self.stats.skipped_videos += 1
            logger.warning(
                "[合并转发视频] 视频段没有可用的 http(s) 链接，已跳过: file=%r",
                data.get("file"),
            )
            return

        self.stats.urls.append(url)

    def _collect_from_cq(self, text: str) -> None:
        for match in _CQ_VIDEO_RE.finditer(text):
            params = dict(_CQ_PARAM_RE.findall(match.group(1)))
            data = {key: _unescape_cq(value) for key, value in params.items()}
            self._add_video(data)


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class ForwardVideoPlugin(Star):
    """引用合并转发后，逐条转发其中视频的插件。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        self._last_used: dict[str, float] = {}
        logger.info(
            "[合并转发视频] 插件初始化完成：send_summary=%s, interval_seconds=%s",
            self._value("send_summary", True),
            self._value("interval_seconds", 3),
        )

    # -- 配置读取 ---------------------------------------------------------

    def _value(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _error_tip_enabled(self) -> bool:
        return _to_bool(self._value("error_tip", True), True)

    def _summary_enabled(self) -> bool:
        return _to_bool(self._value("send_summary", True), True)

    def _stop_requested(self, event: AstrMessageEvent) -> bool:
        """是否收到了 AstrBot 的停止请求（/stop、/reset、/new 等）。

        注意：插件在 handler 入口主动调用过 event.stop_event()，所以不能把
        event.is_stopped() 当作停止请求，否则会误判。这里只检查 AstrBot
        active_event_registry 注入的标记和停止信号。
        """
        try:
            if event.get_extra("agent_stop_requested"):
                return True
            signal = event.get_extra("_background_stop_signal")
            if signal is not None and getattr(signal, "is_set", None):
                return bool(signal.is_set())
        except Exception:
            pass
        return False

    async def _wait_with_stop(
        self, event: AstrMessageEvent, seconds: float
    ) -> bool:
        """可被 /stop 打断的等待；返回 False 表示收到了停止请求。"""
        if seconds <= 0:
            return not self._stop_requested(event)
        deadline = time.monotonic() + seconds
        while True:
            if self._stop_requested(event):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(0.5, remaining))

    def _summary_text(
        self,
        collector: VideoCollector,
        total: int,
        success: int,
        failure: int,
        unsent: int,
    ) -> str:
        text = f"共{total}条，成功{success}条，失败{failure}条"
        if unsent > 0:
            text += f"，已停止，剩余{unsent}条未发送"
        text += "。"
        if collector.stats.fetch_errors:
            text += f"另有 {collector.stats.fetch_errors} 个子转发获取失败。"
        if collector.stats.node_limited or collector.stats.depth_limited:
            text += "部分内容因层级或数量限制未处理。"
        return text

    # -- 权限 / 冷却 ------------------------------------------------------

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """私聊按开关；群聊必须开启总开关并命中白名单。"""
        group_id = ""
        try:
            group_id = _as_str(event.get_group_id())
        except Exception:
            group_id = ""
        if not group_id:
            group_id = _as_str(getattr(getattr(event, "message_obj", None), "group_id", ""))

        if group_id:
            if not _to_bool(self._value("group_enabled", False), False):
                return False
            whitelist = self._value("group_whitelist", []) or []
            if not isinstance(whitelist, (list, tuple, set)):
                whitelist = [whitelist]
            allowed_groups = {
                _as_str(item) for item in whitelist if _as_str(item)
            }
            return group_id in allowed_groups

        return _to_bool(self._value("private_enabled", True), True)

    def _check_cooldown(self, event: AstrMessageEvent) -> bool:
        seconds = _to_float(self._value("cooldown_seconds", 0), 0.0)
        if seconds <= 0:
            return True
        try:
            sender_id = _as_str(event.get_sender_id())
        except Exception:
            sender_id = ""
        if not sender_id:
            return True

        now = time.monotonic()
        last_used = self._last_used.get(sender_id)
        if last_used is not None and now - last_used < seconds:
            return False

        # 简单清理，避免长期运行后字典无限增长
        if len(self._last_used) > 1000:
            self._last_used = {
                key: value
                for key, value in self._last_used.items()
                if now - value < seconds
            }
        self._last_used[sender_id] = now
        return True

    # -- 协议端调用 -------------------------------------------------------

    async def _call_action(
        self, event: AstrMessageEvent, action: str, **params: Any
    ) -> Any:
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("当前事件没有 OneBot 客户端")

        api = getattr(bot, "api", bot)
        timeout = _to_float(self._value("api_timeout_seconds", 10), 10.0)
        if timeout <= 0:
            timeout = 10.0
        return await asyncio.wait_for(
            api.call_action(action, **params), timeout=timeout
        )

    async def _try_get_forward(
        self, event: AstrMessageEvent, forward_id: str
    ) -> list[Any] | None:
        """尝试直接按 id 获取合并转发；失败返回 None。"""
        try:
            response = await self._call_action(
                event, "get_forward_msg", id=forward_id
            )
        except Exception as exc:
            logger.warning(
                "[合并转发视频] get_forward_msg(%s) 失败: %s", forward_id, exc
            )
            return None
        return extract_messages(response)

    async def _load_forward_messages(
        self, event: AstrMessageEvent, reply_id: str
    ) -> tuple[str, list[Any]]:
        """把引用的消息解析成合并转发节点列表。

        返回 (status, messages)：
        - ok：成功拿到节点列表（可能为空）；
        - ignored：引用的是普通消息，或解析不到合并转发；
        - error：协议端调用失败。
        """
        # 快路径：AstrBot 适配器已经把引用消息解析进 Reply.chain
        parsed_forward_id = find_forward_id_in_parsed_message(
            getattr(event, "message_obj", None)
        )
        if parsed_forward_id:
            logger.debug(
                "[合并转发视频] Reply.chain 快路径：forward_id=%s",
                parsed_forward_id,
            )
            messages = await self._try_get_forward(event, parsed_forward_id)
            if messages is not None:
                return "ok", messages

        get_msg_payload: Any = None
        get_msg_ok = False
        try:
            get_msg_payload = await self._call_action(
                event, "get_msg", message_id=reply_id
            )
            get_msg_ok = True
        except Exception as exc:
            logger.warning(
                "[合并转发视频] get_msg(%s) 失败，尝试直接获取转发: %s",
                reply_id,
                exc,
            )

        if get_msg_ok:
            forward_id, inline = find_forward_reference(get_msg_payload)
            if inline is not None:
                return "ok", extract_messages({"messages": inline})
            if forward_id:
                logger.debug(
                    "[合并转发视频] get_msg 解析到 forward_id=%s", forward_id
                )
                messages = await self._try_get_forward(event, forward_id)
                if messages is None:
                    # 某些协议端用消息 id 也能取到同一个转发，兜底一次
                    messages = await self._try_get_forward(event, reply_id)
                return ("ok", messages) if messages is not None else ("error", [])

            # get_msg 成功但没有 forward 段：可能 reply_id 本身就是转发 id
            messages = await self._try_get_forward(event, reply_id)
            if messages is not None:
                return "ok", messages
            return "ignored", []

        # get_msg 整体失败：直接按 reply_id 尝试 get_forward_msg
        messages = await self._try_get_forward(event, reply_id)
        if messages is not None:
            return "ok", messages
        return "error", []

    # -- 发送 -------------------------------------------------------------

    async def _send_plain(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception:
            logger.exception("[合并转发视频] 发送提示消息失败")

    def _no_video_tip(self, collector: VideoCollector) -> str:
        stats = collector.stats
        if stats.fetch_errors:
            return "部分子转发获取失败，没有找到可发送的视频。"
        if stats.skipped_videos:
            return "合并转发里的视频缺少可用的下载链接，无法发送。"
        if stats.node_limited or stats.depth_limited:
            return "没有找到可发送的视频（合并转发内容过多或嵌套层级过深）。"
        return _as_str(
            self._value("no_video_tip", "合并转发里没有视频")
        ) or "合并转发里没有视频"

    # -- 指令 -------------------------------------------------------------

    @filter.command("提取视频")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def extract_videos(self, event: AstrMessageEvent):
        """引用一条合并转发并发送 /提取视频，逐条转发其中的视频。"""
        # 重要：AstrBot 的 ProcessStage 在“handler 没有发送任何消息”且消息为
        # 指令唤醒时，仍会继续调用 LLM。这里显式终止事件传播，确保没有引用、
        # 群未授权、冷却中等静默分支也不会被 AI 回复。
        event.stop_event()

        sender_id = _as_str(getattr(event, "get_sender_id", lambda: "")())
        group_id = _as_str(getattr(event, "get_group_id", lambda: "")())
        logger.info(
            "[合并转发视频] 指令触发：sender=%s, group=%s",
            sender_id,
            group_id or "私聊",
        )

        if not self._is_allowed(event):
            logger.info("[合并转发视频] 群聊未启用或不在白名单，忽略")
            return

        raw_event = getattr(
            getattr(event, "message_obj", None), "raw_message", None
        )
        reply_id = extract_reply_id(raw_event)
        if not reply_id:
            logger.info("[合并转发视频] 没有引用消息，忽略")
            return

        if not self._check_cooldown(event):
            logger.info("[合并转发视频] 冷却中，忽略")
            return

        logger.debug("[合并转发视频] 引用消息 id=%s", reply_id)
        status, messages = await self._load_forward_messages(event, reply_id)
        if status == "ignored":
            logger.info("[合并转发视频] 引用的不是合并转发，忽略")
            return
        if status == "error":
            logger.warning("[合并转发视频] 获取合并转发失败")
            if self._error_tip_enabled():
                await self._send_plain(event, "获取合并转发失败，请稍后再试。")
            return

        logger.info("[合并转发视频] 获取到 %d 个转发节点", len(messages))
        collector = VideoCollector(
            call_api=lambda action, **params: self._call_action(
                event, action, **params
            ),
            max_videos=_to_int(self._value("max_videos", 15), 15),
            max_depth=_to_int(self._value("max_depth", 2), 2),
            max_nodes=_to_int(self._value("max_nodes", 500), 500),
            should_stop=lambda: self._stop_requested(event),
        )
        try:
            await collector.walk_messages(messages)
        except Exception:
            logger.exception("[合并转发视频] 遍历合并转发时出现异常")

        urls = collector.stats.urls
        logger.info(
            "[合并转发视频] 找到 %d 条视频，遍历 %d 个节点",
            len(urls),
            collector.stats.visited,
        )

        if not urls:
            if collector.stats.stop_requested or self._stop_requested(event):
                logger.info("[合并转发视频] 查找过程中收到停止请求，已取消")
                return
            tip = self._no_video_tip(collector)
            logger.info("[合并转发视频] 没有可发送的视频：%s", tip)
            await self._send_plain(event, tip)
            return

        interval = max(0.0, _to_float(self._value("interval_seconds", 3), 3.0))
        attempted = 0
        failed = 0
        stopped = False
        for index, url in enumerate(urls):
            if self._stop_requested(event):
                stopped = True
                logger.info(
                    "[合并转发视频] 收到停止请求，已发送 %d/%d 条",
                    attempted,
                    len(urls),
                )
                break
            if index > 0 and interval > 0:
                if not await self._wait_with_stop(event, interval):
                    stopped = True
                    logger.info(
                        "[合并转发视频] 等待间隔时收到停止请求，已发送 %d/%d 条",
                        attempted,
                        len(urls),
                    )
                    break

            logger.debug(
                "[合并转发视频] 发送第 %d/%d 条视频", index + 1, len(urls)
            )
            attempted += 1
            try:
                await event.send(MessageChain([Video.fromURL(url)]))
                logger.debug(
                    "[合并转发视频] 第 %d/%d 条发送成功", index + 1, len(urls)
                )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "[合并转发视频] 第 %d/%d 条发送失败: %s",
                    index + 1,
                    len(urls),
                    exc,
                )

        total = len(urls) + collector.stats.skipped_videos
        success = attempted - failed
        failure = failed + collector.stats.skipped_videos
        unsent = len(urls) - attempted

        if stopped:
            logger.info(
                "[合并转发视频] 发送已停止：成功 %d 条，失败 %d 条，未发送 %d 条",
                success,
                failure,
                unsent,
            )

        if self._summary_enabled():
            summary = self._summary_text(
                collector,
                total=total,
                success=success,
                failure=failure,
                unsent=unsent,
            )
            logger.info("[合并转发视频] 发送汇总：%s", summary)
            await self._send_plain(event, summary)
            return

        # 未开启汇总时，退回按需提示
        total_failed = failure + collector.stats.fetch_errors
        if total_failed > 0 and self._error_tip_enabled():
            await self._send_plain(
                event,
                f"视频发送完成：成功 {success} 条，失败 {total_failed} 条。",
            )
        if stopped and self._error_tip_enabled():
            await self._send_plain(
                event, f"已停止：已发送 {attempted}/{len(urls)} 条。"
            )
        if (
            (collector.stats.node_limited or collector.stats.depth_limited)
            and self._error_tip_enabled()
        ):
            await self._send_plain(
                event, "合并转发内容过多或嵌套层级过深，已只处理前面的部分。"
            )
