import io
import json

import httpx

from sec_xbrl_warehouse.mcp_server import TOOLS, WarehouseTools, handle, serve


def tools_with(responder):
    seen = []

    def transport(request):
        seen.append(request)
        return responder(request)

    client = httpx.Client(base_url="http://api", transport=httpx.MockTransport(transport))
    return WarehouseTools(client), seen


def call(tools, name, arguments):
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    message["params"] = {"name": name, "arguments": arguments}
    return handle(tools, message)["result"]


def test_handshake_lists_every_tool_and_ignores_notifications():
    tools, _ = tools_with(lambda request: httpx.Response(200, json={}))
    stdin = io.StringIO(
        "\n".join(
            json.dumps(m)
            for m in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
            )
        )
        + "\nnot json\n"
    )
    stdout = io.StringIO()
    serve(tools, stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, None]
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert {t["name"] for t in replies[1]["result"]["tools"]} == set(tools.handlers)
    assert len(TOOLS) == len(tools.handlers)
    assert replies[2]["error"]["code"] == -32601
    assert replies[3]["error"]["code"] == -32700


def test_tools_map_to_api_requests():
    tools, seen = tools_with(lambda request: httpx.Response(200, json={"items": []}))
    call(
        tools,
        "get_financial_series",
        {
            "company": "aapl",
            "metrics": ["revenue", "net_income"],
            "period_types": ["annual", "instant"],
        },
    )
    call(tools, "get_financial_series", {"company": "320193", "metrics": ["revenue"]})
    call(tools, "get_filing_text_blocks", {"accession": "0000320193-25-000079", "policy": True})
    call(tools, "query_raw_facts", {"cik": 320193, "concept_name": "Revenues", "unknown": 1})
    urls = [str(r.url) for r in seen]
    assert urls[0] == (
        "http://api/v1/series?metrics=revenue%2Cnet_income&period_type=annual"
        "&period_type=instant&ticker=aapl"
    )
    assert urls[1] == "http://api/v1/series/320193?metrics=revenue"
    assert urls[2] == "http://api/v1/filings/0000320193-25-000079/text-blocks?policy=true"
    assert urls[3] == "http://api/v1/facts?cik=320193&concept_name=Revenues"


def test_errors_are_returned_to_the_agent_not_raised():
    tools, seen = tools_with(
        lambda request: httpx.Response(422, json={"detail": "Invalid metric code"})
    )
    result = call(tools, "get_financial_series", {"company": "AAPL", "metrics": ["Bad"]})
    assert result["isError"] and "422: Invalid metric code" in result["content"][0]["text"]
    assert call(tools, "get_filing", {"accession": "../health"})["isError"]
    assert call(tools, "list_company_filings", {"cik": "x"})["isError"]
    assert call(tools, "no_such_tool", {})["isError"]
    assert len(seen) == 1
