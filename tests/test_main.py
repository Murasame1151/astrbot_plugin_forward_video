"""不依赖 AstrBot 运行时的解析逻辑测试。

通过注入最小化的 astrbot 模块桩，直接加载 main.py，验证：
- 引用事件中的 reply id 提取；
- get_msg -> forward id 解析；
- 嵌套合并转发、深度/数量限制、CQ 码、缺失链接；
- 私聊/群聊白名单开关；
- get_msg 失败与引用普通消息的分支。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = PLUGIN_DIR.parent


def _install_astrbot_stubs() -> None:
    """为 main.py 的 import 提供最小可用的 astrbot 模块。"""
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []  # type: ignore[attr-defined]

    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("forward-video-test")

    class AstrBotConfig(dict):
        pass

    api.AstrBotConfig = AstrBotConfig

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    class _PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"
        ALL = "all"

    class _Filter:
        PlatformAdapterType = _PlatformAdapterType

        @staticmethod
        def command(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def platform_adapter_type(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain

    components = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text):
            self.text = text

    class Video:
        def __init__(self, url=None):
            self.url = url

        @classmethod
        def fromURL(cls, url):
            return cls(url)

    components.Plain = Plain
    components.Video = Video

    star_mod = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    star_mod.Context = Context
    star_mod.Star = Star

    astrbot.api = api
    api.event = event_mod
    api.message_components = components
    api.star = star_mod

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.message_components": components,
            "astrbot.api.star": star_mod,
        }
    )


_install_astrbot_stubs()

_spec = importlib.util.spec_from_file_location(
    "forward_video_plugin_main", PLUGIN_DIR / "main.py"
)
assert _spec is not None and _spec.loader is not None
main = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = main
_spec.loader.exec_module(main)


# 模拟 get_msg 的返回：引用 id 860342939 指向一条 forward 消息
GET_MSG_RESPONSE = {
    "message_id": 860342939,
    "message": [
        {"type": "forward", "data": {"id": "7700217566943336200"}}
    ],
    "raw_message": "[CQ:forward,id=7700217566943336200]",
}

# 模拟 get_forward_msg 的返回：包含普通视频、嵌套转发中的视频、更深一层嵌套
FORWARD_RESPONSE = {
    "status": "ok",
    "retcode": 0,
    "data": {
        "messages": [
            {
                "sender": {"nickname": "a"},
                "message": [
                    {"type": "text", "data": {"text": "hi"}},
                    {
                        "type": "video",
                        "data": {
                            "file": "a.mp4",
                            "url": "https://example.com/a.mp4",
                            "file_size": "650066",
                        },
                    },
                ],
            },
            {
                "sender": {"nickname": "b"},
                "message": [
                    {
                        "type": "forward",
                        "data": {
                            "content": [
                                {
                                    "sender": {"nickname": "c"},
                                    "message": [
                                        {
                                            "type": "video",
                                            "data": {
                                                "file": "b.mp4",
                                                "url": "https://example.com/b.mp4",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "message": [
                                        {
                                            "type": "forward",
                                            "data": {
                                                "content": [
                                                    {
                                                        "message": [
                                                            {
                                                                "type": "video",
                                                                "data": {
                                                                    "url": "https://example.com/c.mp4"
                                                                },
                                                            }
                                                        ]
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                },
                            ]
                        },
                    }
                ],
            },
        ]
    },
}


class FakeComponent:
    def __init__(self, component_type, **kwargs):
        self.type = component_type
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeEnum:
    def __init__(self, value):
        self.value = value


class FakeEvent:
    def __init__(
        self,
        group_id="",
        sender_id="10001",
        raw_event=None,
        api=None,
        parsed_message=None,
    ):
        self._group_id = group_id
        self._sender_id = sender_id
        self.message_obj = types.SimpleNamespace(
            group_id=group_id,
            raw_message=raw_event or {},
            message_id="1882400482",
            message=parsed_message or [],
        )
        self.bot = types.SimpleNamespace(api=api) if api is not None else None
        self.sent = []
        self.stopped = False
        self._extras = {}

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def stop_event(self):
        self.stopped = True

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    async def send(self, chain):
        self.sent.append(chain)


class ReplyParsingTest(unittest.TestCase):
    def test_extract_reply_id_from_message_segments(self):
        event = {
            "message": [
                {"type": "reply", "data": {"id": "860342939"}},
                {"type": "text", "data": {"text": "/提取视频"}},
            ],
            "raw_message": "[CQ:reply,id=860342939]/提取视频",
        }
        self.assertEqual(main.extract_reply_id(event), "860342939")

    def test_extract_reply_id_from_cq_fallback(self):
        event = {"raw_message": "[CQ:reply,id=123456]/提取视频"}
        self.assertEqual(main.extract_reply_id(event), "123456")

    def test_extract_reply_id_without_reply(self):
        event = {"message": [{"type": "text", "data": {"text": "/提取视频"}}]}
        self.assertIsNone(main.extract_reply_id(event))

    def test_extract_reply_id_from_real_sample(self):
        sample = WORKSPACE_DIR / "3.json"
        if not sample.exists():
            self.skipTest("3.json not found")
        event = json.loads(sample.read_text(encoding="utf-8"))
        self.assertEqual(main.extract_reply_id(event), "860342939")


class ParsedMessageTest(unittest.TestCase):
    def test_component_type_name_handles_str_enum(self):
        segment = FakeComponent(FakeEnum("Reply"))
        self.assertEqual(main._component_type_name(segment), "reply")

    def test_find_forward_id_in_reply_chain(self):
        reply = FakeComponent(
            FakeEnum("Reply"),
            chain=[FakeComponent(FakeEnum("Forward"), id="7700217566943336200")],
        )
        message_obj = types.SimpleNamespace(message=[reply])
        self.assertEqual(
            main.find_forward_id_in_parsed_message(message_obj),
            "7700217566943336200",
        )

    def test_find_forward_id_ignores_non_reply(self):
        message_obj = types.SimpleNamespace(
            message=[FakeComponent(FakeEnum("Plain"), text="hi")]
        )
        self.assertIsNone(main.find_forward_id_in_parsed_message(message_obj))


class ForwardReferenceTest(unittest.TestCase):
    def test_find_forward_id_from_get_msg(self):
        self.assertEqual(
            main.find_forward_reference(GET_MSG_RESPONSE),
            ("7700217566943336200", None),
        )

    def test_find_forward_id_from_cq_string(self):
        payload = {"raw_message": "[CQ:forward,id=abc123]"}
        self.assertEqual(
            main.find_forward_reference(payload), ("abc123", None)
        )

    def test_find_inline_content(self):
        payload = {
            "message": [
                {
                    "type": "forward",
                    "data": {"content": [{"message": []}]},
                }
            ]
        }
        forward_id, content = main.find_forward_reference(payload)
        self.assertIsNone(forward_id)
        self.assertEqual(content, [{"message": []}])

    def test_find_forward_id_from_napcat_raw_parent(self):
        payload = {
            "message": [],
            "raw": {
                "msgType": 8,
                "elements": [{"multiForwardMsgElement": {"fileName": "x"}}],
                "parentMsgIdList": ["7700217566943336200"],
            },
        }
        self.assertEqual(
            main.find_forward_reference(payload),
            ("7700217566943336200", None),
        )

    def test_raw_parent_ignored_for_normal_message(self):
        payload = {
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "raw": {"msgType": 9, "parentMsgIdList": ["123"]},
        }
        self.assertEqual(main.find_forward_reference(payload), (None, None))

    def test_plain_message_has_no_forward(self):
        payload = {"message": [{"type": "text", "data": {"text": "hello"}}]}
        self.assertEqual(main.find_forward_reference(payload), (None, None))

    def test_extract_messages_unwraps_envelope(self):
        messages = main.extract_messages(FORWARD_RESPONSE)
        self.assertEqual(len(messages), 2)
        self.assertIn("message", messages[0])

    def test_extract_messages_direct_payload(self):
        payload = {"messages": [{"message": []}]}
        self.assertEqual(main.extract_messages(payload), [{"message": []}])

    def test_extract_messages_double_wrapped(self):
        payload = {"messages": {"messages": [{"message": []}]}}
        self.assertEqual(main.extract_messages(payload), [{"message": []}])


class VideoCollectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_collect_videos_including_nested(self):
        collector = main.VideoCollector(
            call_api=self._unexpected_api, max_videos=0, max_depth=2
        )
        await collector.walk_messages(main.extract_messages(FORWARD_RESPONSE))
        self.assertEqual(
            collector.stats.urls,
            [
                "https://example.com/a.mp4",
                "https://example.com/b.mp4",
                "https://example.com/c.mp4",
            ],
        )
        self.assertFalse(collector.stats.depth_limited)

    async def test_max_depth_excludes_deeper_level(self):
        collector = main.VideoCollector(
            call_api=self._unexpected_api, max_videos=0, max_depth=1
        )
        await collector.walk_messages(main.extract_messages(FORWARD_RESPONSE))
        self.assertEqual(
            collector.stats.urls,
            [
                "https://example.com/a.mp4",
                "https://example.com/b.mp4",
            ],
        )
        self.assertTrue(collector.stats.depth_limited)

    async def test_max_videos_stops_collection(self):
        collector = main.VideoCollector(
            call_api=self._unexpected_api, max_videos=1, max_depth=2
        )
        await collector.walk_messages(main.extract_messages(FORWARD_RESPONSE))
        self.assertEqual(collector.stats.urls, ["https://example.com/a.mp4"])

    async def test_nested_forward_by_id_calls_api(self):
        calls = []

        async def fake_api(action, **params):
            calls.append((action, params))
            return {
                "data": {
                    "messages": [
                        {
                            "message": [
                                {
                                    "type": "video",
                                    "data": {"url": "https://example.com/nested.mp4"},
                                }
                            ]
                        }
                    ]
                }
            }

        collector = main.VideoCollector(
            call_api=fake_api, max_videos=0, max_depth=2
        )
        await collector.walk_messages(
            [{"message": [{"type": "forward", "data": {"id": "999"}}]}]
        )
        self.assertEqual(
            collector.stats.urls, ["https://example.com/nested.mp4"]
        )
        self.assertEqual(calls, [("get_forward_msg", {"id": "999"})])

    async def test_cq_string_video(self):
        collector = main.VideoCollector(
            call_api=self._unexpected_api, max_videos=0, max_depth=2
        )
        await collector.walk_segments(
            "[CQ:video,file=x.mp4,url=https://example.com/cq.mp4]"
        )
        self.assertEqual(
            collector.stats.urls, ["https://example.com/cq.mp4"]
        )

    async def test_video_without_url_is_skipped(self):
        collector = main.VideoCollector(
            call_api=self._unexpected_api, max_videos=0, max_depth=2
        )
        await collector.walk_messages(
            [{"message": [{"type": "video", "data": {"file": "x.mp4"}}]}]
        )
        self.assertEqual(collector.stats.urls, [])
        self.assertEqual(collector.stats.skipped_videos, 1)

    async def test_no_duplicate_api_call_for_same_forward_id(self):
        calls = []

        async def fake_api(action, **params):
            calls.append(params.get("id"))
            return {"data": {"messages": [{"message": []}]}}

        collector = main.VideoCollector(
            call_api=fake_api, max_videos=0, max_depth=3
        )
        await collector.walk_messages(
            [
                {"message": [{"type": "forward", "data": {"id": "same"}}]},
                {"message": [{"type": "forward", "data": {"id": "same"}}]},
            ]
        )
        self.assertEqual(calls, ["same"])

    async def _unexpected_api(self, action, **params):
        raise AssertionError(f"unexpected api call: {action} {params}")


class PermissionTest(unittest.TestCase):
    def _plugin(self, **config):
        return main.ForwardVideoPlugin(context=None, config=config)

    def test_private_default_enabled(self):
        self.assertTrue(self._plugin()._is_allowed(FakeEvent(group_id="")))

    def test_private_can_be_disabled(self):
        self.assertFalse(
            self._plugin(private_enabled=False)._is_allowed(FakeEvent())
        )

    def test_group_default_disabled(self):
        self.assertFalse(
            self._plugin()._is_allowed(FakeEvent(group_id="123456"))
        )

    def test_group_requires_whitelist(self):
        plugin = self._plugin(
            group_enabled=True, group_whitelist=["123456", "7890"]
        )
        self.assertTrue(plugin._is_allowed(FakeEvent(group_id="123456")))
        self.assertFalse(plugin._is_allowed(FakeEvent(group_id="999999")))

    def test_empty_whitelist_denies_all_groups(self):
        plugin = self._plugin(group_enabled=True, group_whitelist=[])
        self.assertFalse(plugin._is_allowed(FakeEvent(group_id="123456")))

    def test_integer_whitelist_items(self):
        plugin = self._plugin(group_enabled=True, group_whitelist=[123456])
        self.assertTrue(plugin._is_allowed(FakeEvent(group_id="123456")))

    def test_cooldown(self):
        plugin = self._plugin(cooldown_seconds=60)
        event = FakeEvent()
        self.assertTrue(plugin._check_cooldown(event))
        self.assertFalse(plugin._check_cooldown(event))


class LoadForwardTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_msg_then_get_forward_msg(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return GET_MSG_RESPONSE
                if action == "get_forward_msg":
                    self.forward_id = params.get("id")
                    return FORWARD_RESPONSE
                raise AssertionError(action)

        api = FakeAPI()
        plugin = main.ForwardVideoPlugin(context=None, config={})
        event = FakeEvent(api=api)
        status, messages = await plugin._load_forward_messages(event, "860342939")
        self.assertEqual(status, "ok")
        self.assertEqual(len(messages), 2)
        self.assertEqual(api.forward_id, "7700217566943336200")

    async def test_get_msg_failure_falls_back_to_forward_id(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    raise RuntimeError("message not found")
                if action == "get_forward_msg":
                    self.forward_id = params.get("id")
                    return FORWARD_RESPONSE
                raise AssertionError(action)

        api = FakeAPI()
        plugin = main.ForwardVideoPlugin(context=None, config={})
        event = FakeEvent(api=api)
        status, messages = await plugin._load_forward_messages(event, "7700217566943336200")
        self.assertEqual(status, "ok")
        self.assertEqual(api.forward_id, "7700217566943336200")
        self.assertEqual(len(messages), 2)

    async def test_parsed_reply_chain_skips_get_msg(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    raise AssertionError("get_msg should not be called")
                if action == "get_forward_msg":
                    self.forward_id = params.get("id")
                    return FORWARD_RESPONSE
                raise AssertionError(action)

        api = FakeAPI()
        plugin = main.ForwardVideoPlugin(context=None, config={})
        reply = FakeComponent(
            FakeEnum("Reply"),
            chain=[FakeComponent(FakeEnum("Forward"), id="7700217566943336200")],
        )
        event = FakeEvent(
            api=api,
            raw_event={
                "message": [
                    {"type": "reply", "data": {"id": "860342939"}},
                ],
            },
            parsed_message=[reply],
        )
        status, messages = await plugin._load_forward_messages(event, "860342939")
        self.assertEqual(status, "ok")
        self.assertEqual(len(messages), 2)
        self.assertEqual(api.forward_id, "7700217566943336200")

    async def test_quoted_plain_message_is_ignored(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return {
                        "message": [{"type": "text", "data": {"text": "hello"}}]
                    }
                if action == "get_forward_msg":
                    raise RuntimeError("not a forward message")
                raise AssertionError(action)

        plugin = main.ForwardVideoPlugin(context=None, config={})
        event = FakeEvent(api=FakeAPI())
        status, messages = await plugin._load_forward_messages(event, "123")
        self.assertEqual(status, "ignored")
        self.assertEqual(messages, [])



class HandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_handler_sends_each_video(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return GET_MSG_RESPONSE
                if action == "get_forward_msg":
                    return FORWARD_RESPONSE
                raise AssertionError(action)

        plugin = main.ForwardVideoPlugin(
            context=None,
            config={
                "interval_seconds": 0,
                "max_videos": 15,
                "max_depth": 2,
                "send_summary": False,
            },
        )
        event = FakeEvent(api=FakeAPI(), raw_event={
            "message": [
                {"type": "reply", "data": {"id": "860342939"}},
                {"type": "text", "data": {"text": "/提取视频"}},
            ],
            "raw_message": "[CQ:reply,id=860342939]/提取视频",
        })
        await plugin.extract_videos(event)
        self.assertEqual(len(event.sent), 3)
        self.assertEqual(
            [chain.chain[0].url for chain in event.sent],
            [
                "https://example.com/a.mp4",
                "https://example.com/b.mp4",
                "https://example.com/c.mp4",
            ],
        )
        self.assertTrue(event.stopped)

    async def test_handler_sends_tip_when_no_video(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return GET_MSG_RESPONSE
                if action == "get_forward_msg":
                    return {
                        "data": {
                            "messages": [
                                {
                                    "message": [
                                        {"type": "text", "data": {"text": "hi"}}
                                    ]
                                }
                            ]
                        }
                    }
                raise AssertionError(action)

        plugin = main.ForwardVideoPlugin(
            context=None, config={"interval_seconds": 0}
        )
        event = FakeEvent(api=FakeAPI(), raw_event={
            "message": [
                {"type": "reply", "data": {"id": "860342939"}},
                {"type": "text", "data": {"text": "/提取视频"}},
            ],
        })
        await plugin.extract_videos(event)
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0].chain[0], main.Plain)
        self.assertTrue(event.stopped)



    async def test_handler_sends_summary(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return GET_MSG_RESPONSE
                if action == "get_forward_msg":
                    return {
                        "data": {
                            "messages": [
                                {
                                    "message": [
                                        {
                                            "type": "video",
                                            "data": {"url": "https://example.com/1.mp4"},
                                        },
                                        {
                                            "type": "video",
                                            "data": {"url": "https://example.com/2.mp4"},
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                raise AssertionError(action)

        plugin = main.ForwardVideoPlugin(
            context=None,
            config={"interval_seconds": 0},
        )
        event = FakeEvent(api=FakeAPI(), raw_event={
            "message": [
                {"type": "reply", "data": {"id": "860342939"}},
                {"type": "text", "data": {"text": "/提取视频"}},
            ],
        })
        await plugin.extract_videos(event)
        self.assertEqual(len(event.sent), 3)
        self.assertIsInstance(event.sent[2].chain[0], main.Plain)
        self.assertEqual(
            event.sent[2].chain[0].text,
            "共2条，成功2条，失败0条。",
        )

    async def test_handler_summary_counts_failure(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return GET_MSG_RESPONSE
                if action == "get_forward_msg":
                    return {
                        "data": {
                            "messages": [
                                {
                                    "message": [
                                        {
                                            "type": "video",
                                            "data": {"url": "https://example.com/ok.mp4"},
                                        },
                                        {
                                            "type": "video",
                                            "data": {"url": "https://example.com/bad.mp4"},
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                raise AssertionError(action)

        class FlakyEvent(FakeEvent):
            async def send(self, chain):
                first = chain.chain[0]
                if getattr(first, "url", None) == "https://example.com/bad.mp4":
                    raise RuntimeError("send failed")
                self.sent.append(chain)

        plugin = main.ForwardVideoPlugin(
            context=None,
            config={"interval_seconds": 0},
        )
        event = FlakyEvent(api=FakeAPI(), raw_event={
            "message": [
                {"type": "reply", "data": {"id": "860342939"}},
                {"type": "text", "data": {"text": "/提取视频"}},
            ],
        })
        await plugin.extract_videos(event)
        self.assertEqual(len(event.sent), 2)
        self.assertIsInstance(event.sent[1].chain[0], main.Plain)
        self.assertEqual(
            event.sent[1].chain[0].text,
            "共2条，成功1条，失败1条。",
        )

    async def test_stop_request_stops_sending_and_summarizes(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return GET_MSG_RESPONSE
                if action == "get_forward_msg":
                    return {
                        "data": {
                            "messages": [
                                {
                                    "message": [
                                        {
                                            "type": "video",
                                            "data": {"url": "https://example.com/1.mp4"},
                                        },
                                        {
                                            "type": "video",
                                            "data": {"url": "https://example.com/2.mp4"},
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                raise AssertionError(action)

        class StopAfterFirstEvent(FakeEvent):
            async def send(self, chain):
                self.sent.append(chain)
                if getattr(chain.chain[0], "url", None):
                    self.set_extra("agent_stop_requested", True)

        plugin = main.ForwardVideoPlugin(
            context=None,
            config={"interval_seconds": 0},
        )
        event = StopAfterFirstEvent(api=FakeAPI(), raw_event={
            "message": [
                {"type": "reply", "data": {"id": "860342939"}},
                {"type": "text", "data": {"text": "/提取视频"}},
            ],
        })
        await plugin.extract_videos(event)
        self.assertEqual(len(event.sent), 2)
        self.assertIsInstance(event.sent[1].chain[0], main.Plain)
        self.assertIn("已停止", event.sent[1].chain[0].text)
        self.assertIn("剩余1条未发送", event.sent[1].chain[0].text)

    def test_stop_request_ignores_self_stop_event(self):
        plugin = main.ForwardVideoPlugin(context=None, config={})
        event = FakeEvent()
        event.stop_event()
        self.assertTrue(event.stopped)
        self.assertFalse(plugin._stop_requested(event))

    async def test_handler_reports_send_failure(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return GET_MSG_RESPONSE
                if action == "get_forward_msg":
                    return {
                        "data": {
                            "messages": [
                                {
                                    "message": [
                                        {
                                            "type": "video",
                                            "data": {"url": "https://example.com/ok.mp4"},
                                        },
                                        {
                                            "type": "video",
                                            "data": {"url": "https://example.com/bad.mp4"},
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                raise AssertionError(action)

        class FlakyEvent(FakeEvent):
            async def send(self, chain):
                first = chain.chain[0]
                if getattr(first, "url", None) == "https://example.com/bad.mp4":
                    raise RuntimeError("send failed")
                self.sent.append(chain)

        plugin = main.ForwardVideoPlugin(
            context=None,
            config={"interval_seconds": 0, "send_summary": False},
        )
        event = FlakyEvent(api=FakeAPI(), raw_event={
            "message": [
                {"type": "reply", "data": {"id": "860342939"}},
                {"type": "text", "data": {"text": "/提取视频"}},
            ],
        })
        await plugin.extract_videos(event)
        self.assertEqual(len(event.sent), 2)
        self.assertIsInstance(event.sent[1].chain[0], main.Plain)
        self.assertIn("失败 1 条", event.sent[1].chain[0].text)
        self.assertTrue(event.stopped)

    async def test_handler_ignores_without_reply(self):
        plugin = main.ForwardVideoPlugin(context=None, config={})
        event = FakeEvent(raw_event={
            "message": [{"type": "text", "data": {"text": "/提取视频"}}],
        })
        await plugin.extract_videos(event)
        self.assertEqual(event.sent, [])
        self.assertTrue(event.stopped)

    async def test_handler_ignores_group_when_disabled(self):
        class FakeAPI:
            async def call_action(self, action, **params):
                raise AssertionError("should not call api")

        plugin = main.ForwardVideoPlugin(context=None, config={})
        event = FakeEvent(
            group_id="123456",
            api=FakeAPI(),
            raw_event={
                "message": [
                    {"type": "reply", "data": {"id": "860342939"}},
                ],
            },
        )
        await plugin.extract_videos(event)
        self.assertEqual(event.sent, [])
        self.assertTrue(event.stopped)


if __name__ == "__main__":
    unittest.main()

