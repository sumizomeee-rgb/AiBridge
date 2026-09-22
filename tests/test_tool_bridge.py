from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from aibridge.protocols import (  # noqa: E402
    CanonicalEvent,
    WEB_PROMPT_MAX_CHARS,
    from_anthropic,
    from_openai,
    flatten_web_prompt,
    parse_web_tool_response,
    to_anthropic_upstream,
    to_openai_upstream,
)
from aibridge.providers import _bridge_web_tools  # noqa: E402


TOOLS = [
    {
        "name": "write_file",
        "description": "Write a UTF-8 text file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    }
]


class ToolBridgeTests(unittest.TestCase):
    def test_openai_prompt_contains_compact_bridge_and_tool_history(self) -> None:
        req = from_openai(
            {
                "model": "deepseek-web",
                "messages": [
                    {"role": "system", "content": "Keep edits scoped."},
                    {"role": "user", "content": "Create a marker."},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_old",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path":"a.txt","content":"ok"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_old", "content": "written"},
                ],
                "tools": [{"type": "function", "function": {"name": "write_file", "description": "Write", "parameters": TOOLS[0]["input_schema"]}}],
            },
            "default",
        )
        prompt = flatten_web_prompt(req)
        self.assertIn("<aibridge_system_constraints>", prompt)
        self.assertIn("<aibridge_tool_protocol>", prompt)
        self.assertIn('"name":"write_file"', prompt)
        self.assertIn("<tool_call_history>", prompt)
        self.assertIn('<tool_result id="call_old">', prompt)

    def test_anthropic_history_preserves_tool_use_and_result(self) -> None:
        req = from_anthropic(
            {
                "model": "deepseek-web",
                "system": "Use tools when needed.",
                "messages": [
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "tool_1", "name": "write_file", "input": {"path": "a.txt", "content": "ok"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool_1", "content": "written"}]},
                ],
                "tools": TOOLS,
            },
            "default",
        )
        prompt = flatten_web_prompt(req)
        self.assertIn('"id":"tool_1"', prompt)
        self.assertIn('<tool_result id="tool_1">', prompt)

    def test_tagged_tool_call_is_parsed_and_removed_from_visible_text(self) -> None:
        raw = '准备写入。\n<aibridge_tool_call>{"name":"write_file","arguments":{"path":"a.txt","content":"ok"}}</aibridge_tool_call>'
        visible, calls = parse_web_tool_response(raw, TOOLS)
        self.assertEqual("准备写入。", visible)
        self.assertEqual("write_file", calls[0]["function"]["name"])
        self.assertEqual({"path": "a.txt", "content": "ok"}, json.loads(calls[0]["function"]["arguments"]))

    def test_unknown_tool_is_not_promoted(self) -> None:
        raw = '<aibridge_tool_call>{"name":"delete_everything","arguments":{}}</aibridge_tool_call>'
        visible, calls = parse_web_tool_response(raw, TOOLS)
        self.assertEqual([], calls)
        self.assertEqual(raw, visible)

    def test_tool_choice_none_does_not_inject_bridge(self) -> None:
        req = from_openai(
            {
                "model": "deepseek-web",
                "messages": [{"role": "user", "content": "Answer without tools."}],
                "tools": [{"type": "function", "function": {"name": "write_file", "parameters": TOOLS[0]["input_schema"]}}],
                "tool_choice": "none",
            },
            "default",
        )
        self.assertNotIn("<aibridge_tool_protocol>", flatten_web_prompt(req))

    def test_json_tool_calls_fallback(self) -> None:
        raw = '{"tool_calls":[{"name":"write_file","arguments":{"path":"a.txt","content":"ok"}}]}'
        visible, calls = parse_web_tool_response(raw, TOOLS)
        self.assertEqual("", visible)
        self.assertEqual("write_file", calls[0]["function"]["name"])

    def test_deepseek_dsml_tool_call(self) -> None:
        raw = '''<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="write_file">
<｜｜DSML｜｜ parameter name="path" string="true">a.txt</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="content" string="true">ok</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>'''
        visible, calls = parse_web_tool_response(raw, TOOLS, "deepseek")
        self.assertEqual("", visible)
        self.assertEqual("write_file", calls[0]["function"]["name"])
        self.assertEqual({"path": "a.txt", "content": "ok"}, json.loads(calls[0]["function"]["arguments"]))

    def test_deepseek_dsml_is_not_parsed_for_other_web_sources(self) -> None:
        raw = '''<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="write_file">
<｜｜DSML｜｜ parameter name="path" string="true">a.txt</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>'''
        visible, calls = parse_web_tool_response(raw, TOOLS)
        self.assertEqual([], calls)
        self.assertEqual(raw, visible)

    def test_long_latest_message_is_preserved_once_without_gateway_truncation(self) -> None:
        body = "TASK-AT-START\n" + ("runtime metadata " * 6000) + "\nRECENT-TAIL"
        req = from_anthropic(
            {
                "model": "deepseek-web",
                "messages": [{"role": "user", "content": body}],
            },
            "default",
        )
        prompt = flatten_web_prompt(req)
        self.assertIn(body, prompt)
        self.assertEqual(1, prompt.count("TASK-AT-START"))
        self.assertEqual(1, prompt.count("RECENT-TAIL"))
        self.assertNotIn("[中间内容由网关压缩]", prompt)

    def test_large_tool_catalog_compacts_prompt_but_keeps_critical_edges(self) -> None:
        large_tools = [
            {
                "name": f"tool_{index}",
                "description": "long description " * 200,
                "input_schema": {
                    "type": "object",
                    "properties": {f"arg_{arg}": {"type": "string", "description": "details " * 100} for arg in range(20)},
                },
            }
            for index in range(20)
        ]
        system = "SYSTEM-START\n" + ("runtime information " * 3000) + "\nSYSTEM-END"
        body = "CURRENT-REAL-TASK\n" + ("diff body " * 10000) + "\nOUTPUT-JSON-ONLY"
        req = from_anthropic(
            {
                "model": "deepseek-web",
                "system": system,
                "messages": [{"role": "user", "content": body}],
                "tools": large_tools,
            },
            "default",
        )
        prompt = flatten_web_prompt(req)
        self.assertLessEqual(len(prompt), WEB_PROMPT_MAX_CHARS)
        self.assertIn("SYSTEM-START", prompt)
        self.assertIn("SYSTEM-END", prompt)
        self.assertIn("CURRENT-REAL-TASK", prompt)
        self.assertIn("OUTPUT-JSON-ONLY", prompt)
        self.assertEqual(1, prompt.count("CURRENT-REAL-TASK"))
        self.assertIn("<aibridge_tool_protocol>", prompt)
        self.assertIn("<aibridge_context_notice>", prompt)
        self.assertIn("[中间内容由网关为稳定性省略]", prompt)

    def test_harupulse_truncation_markers_remain_literal_request_content(self) -> None:
        body = (
            "变更文件：Product/Table/Share/Fuben/TransfiniteTower/Config.tab\n"
            + ("+配置数据\n" * 12000)
            + "[已截断：revision r1647989 的 diff 部分省略]\n"
            + "[已截断：diff 行数超限，后续省略]\n"
            + "只输出 JSON"
        )
        req = from_anthropic(
            {"model": "deepseek-web", "messages": [{"role": "user", "content": body}]},
            "default",
        )
        prompt = flatten_web_prompt(req)
        self.assertIn(body, prompt)
        self.assertEqual(1, prompt.count("revision r1647989"))
        self.assertNotIn("<aibridge_context_notice>", prompt)

    def test_oversized_history_keeps_first_task_recent_tool_result_and_current_input(self) -> None:
        messages = [{"role": "user", "content": "FIRST-TASK：检查引用并做一个小修改"}]
        for index in range(8):
            messages.extend([
                {"role": "assistant", "content": f"OLD-ANSWER-{index}\n" + ("旧回答 " * 5000)},
                {"role": "user", "content": f"OLD-QUESTION-{index}\n" + ("旧问题 " * 5000)},
            ])
        messages.extend([
            {"role": "tool", "tool_call_id": "call_recent", "content": "RECENT-TOOL-RESULT\n" + ("引用结果 " * 3000)},
            {"role": "user", "content": "CURRENT-TASK：根据刚才的引用结果继续"},
        ])
        req = from_openai({"model": "web-auto", "messages": messages}, "auto")

        prompt = flatten_web_prompt(req)

        self.assertLessEqual(len(prompt), WEB_PROMPT_MAX_CHARS)
        self.assertIn("FIRST-TASK", prompt)
        self.assertIn("RECENT-TOOL-RESULT", prompt)
        self.assertIn("CURRENT-TASK", prompt)
        self.assertIn("<aibridge_context_notice>", prompt)
        self.assertNotIn("OLD-ANSWER-0", prompt)

    def test_web_prompt_marks_images_unavailable_without_copying_payload(self) -> None:
        req = from_openai(
            {
                "model": "web-auto",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看看这张图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET_IMAGE"}},
                    ],
                }],
            },
            "auto",
        )

        prompt = flatten_web_prompt(req)

        self.assertIn("看看这张图", prompt)
        self.assertIn('<aibridge_media_unavailable count="1">', prompt)
        self.assertIn("不得声称已经查看", prompt)
        self.assertNotIn("SECRET_IMAGE", prompt)

    def test_real_api_payload_still_preserves_images(self) -> None:
        openai_content = [
            {"type": "text", "text": "识别图片"},
            {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
        ]
        anthropic_content = [
            {"type": "text", "text": "识别图片"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "IMAGE_DATA"}},
        ]
        openai_req = from_openai(
            {"model": "api-model", "messages": [{"role": "user", "content": openai_content}]},
            "upstream",
        )
        anthropic_req = from_anthropic(
            {"model": "api-model", "messages": [{"role": "user", "content": anthropic_content}]},
            "upstream",
        )

        self.assertEqual(openai_content, to_openai_upstream(openai_req, stream=False)["messages"][0]["content"])
        self.assertEqual(anthropic_content, to_anthropic_upstream(anthropic_req, stream=False)["messages"][0]["content"])


class AsyncToolBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_event_stream_becomes_tool_event(self) -> None:
        async def source():
            yield CanonicalEvent("reasoning", reasoning="thinking")
            yield CanonicalEvent("text", text='<aibridge_tool_call>{"name":"write_file",')
            yield CanonicalEvent("text", text='"arguments":{"path":"a.txt","content":"ok"}}</aibridge_tool_call>')
            yield CanonicalEvent("usage", usage={"output_tokens": 12})

        req = from_anthropic({"model": "deepseek-web", "messages": [], "tools": TOOLS}, "default")
        events = [event async for event in _bridge_web_tools(source(), req)]
        self.assertEqual(["reasoning", "usage", "tool"], [event.type for event in events])
        self.assertEqual("write_file", events[-1].tool["function"]["name"])


if __name__ == "__main__":
    unittest.main()
