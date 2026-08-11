"""DeepSeek 官方端点 thinking 模式的 reasoning_content 回传契约。

真机事故(2026-08-11, WorkBuddy code 全量, deepseek-v4-flash 官方端点):
模型偶尔在连续操作的快轮次返回**不带 reasoning_content 的 tool_call 响应**;
这条助手消息进历史后,下一个请求被服务端 400 拒绝:
    "The `reasoning_content` in the thinking mode must be passed back to the API."
240 个 attempt 里 218 个(91%)被它提前掐断。

对官方 API 的契约探针(2026-08-11 实测):
    assistant 消息带非空 reasoning_content     -> 200
    assistant 消息带 reasoning_content=""      -> 200
    assistant 消息缺 reasoning_content 键      -> 400

修法:请求清洗时,凡本次消息列表里已有任何助手消息携带 reasoning_content
(即对话明显处于 thinking 模式),就给缺键的助手消息补 reasoning_content=""。
非 thinking 对话(整个列表都没有这个键)保持原样,不新增字段。
这同时覆盖 prune_old_reasoning(紧急收缩)剥掉老推理后的请求。
"""

from __future__ import annotations

from raven.providers.litellm_provider import LiteLLMProvider


def _sanitize(messages):
    return LiteLLMProvider._sanitize_messages(messages)


def test_assistant_missing_reasoning_is_backfilled_with_empty_string() -> None:
    messages = [
        {"role": "user", "content": "fix the bug"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "let me look around",
            "tool_calls": [{"id": "call00001", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call00001", "content": "src/"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call00002", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call00002", "content": "..."},
    ]
    out = _sanitize(messages)
    assert out[1]["reasoning_content"] == "let me look around"
    assert out[3]["reasoning_content"] == "", "thinking 对话里缺键的助手消息必须补空串,否则官方端点 400"


def test_pruned_reasoning_history_is_backfilled() -> None:
    """紧急收缩会 pop 掉老助手消息的 reasoning_content,回传时同样要补键。"""
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old turn, reasoning pruned", "tool_calls": []},
        {"role": "assistant", "content": "", "reasoning_content": "fresh reasoning"},
    ]
    out = _sanitize(messages)
    assert out[1]["reasoning_content"] == ""
    assert out[2]["reasoning_content"] == "fresh reasoning"


def test_non_thinking_conversations_gain_no_new_keys() -> None:
    """整个列表没有任何 reasoning_content 时保持原样——别给非 thinking 端点塞新字段。"""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "assistant", "content": "", "tool_calls": []},
    ]
    out = _sanitize(messages)
    for m in out:
        assert "reasoning_content" not in m


def test_reasoning_none_counts_as_missing_and_is_backfilled() -> None:
    """键在但值是 None 等价于缺键(服务端按缺失处理),同样补成空串。"""
    messages = [
        {"role": "assistant", "content": "", "reasoning_content": "thought"},
        {"role": "assistant", "content": "", "reasoning_content": None},
    ]
    out = _sanitize(messages)
    assert out[1]["reasoning_content"] == ""
