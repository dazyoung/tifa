"""The MCP gate counts tools/call requests without ever raising on hostile bodies."""

from tifa.api.app import mcp_tool_calls


def test_counts_single_and_batched_tool_calls():
    assert mcp_tool_calls(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}') == 1
    batch = b'[{"method":"tools/call"},{"method":"tools/list"},{"method":"tools/call"}]'
    assert mcp_tool_calls(batch) == 2


def test_bad_bodies_count_as_zero():
    assert mcp_tool_calls(b"not json") == 0
    assert mcp_tool_calls(b"\xff\xfe") == 0
    assert mcp_tool_calls(b'"a string"') == 0


def test_deeply_nested_body_does_not_escape():
    body = b"[" * 100_000 + b"]" * 100_000
    assert mcp_tool_calls(body) == 0
