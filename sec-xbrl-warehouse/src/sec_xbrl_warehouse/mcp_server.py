"""Model Context Protocol server exposing the read API as tools (stdio transport).

The server is an HTTP client of the FastAPI query API, never of the databases, so an
agent sees exactly what the `xbrl_api` reader sees, under the same key, query budget,
and limits. Configure it with `XBRL_MCP_API_URL` (default http://127.0.0.1:8080) and,
when the API requires one, `XBRL_API_KEY`.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import IO, Any

import httpx

from sec_xbrl_warehouse import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_RESULT_CHARS = 200_000

Params = dict[str, Any]


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _integer(description: str, minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "description": description, "minimum": minimum, "maximum": maximum}


ACCESSION = _string("Filing key, e.g. the SEC accession 0000320193-25-000079.")
CIK = _integer("SEC Central Index Key, e.g. 320193 for Apple.", 0, 9_999_999_999)
PERIOD_TYPES = {
    "type": "array",
    "items": {"type": "string", "enum": ["annual", "quarter", "ttm", "instant"]},
    "description": "Period types to return (default all). Balance-sheet metrics are instant.",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_companies",
        "description": "Find companies loaded in the warehouse by ticker, CIK, or part of the name. "
        "Use this first to get a company's CIK.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": _string("Ticker, CIK, or name text, e.g. AAPL or Apple."),
                "limit": _integer("Maximum results.", 1, 100),
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_metrics",
        "description": "List the standardized metric codes (revenue, net_income, free_cash_flow, "
        "total_assets, ...) accepted by get_financial_series.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_financial_series",
        "description": "Standardized annual, quarterly, trailing-twelve-month, and instant values "
        "for one company. Values are in the reported unit (usually USD, not millions). "
        "Points are ordered by metric and period end, oldest first, and list the concept and "
        "source filings each came from.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "company": _string("Ticker (AAPL) or CIK (320193)."),
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "description": "Metric codes from list_metrics.",
                },
                "period_types": PERIOD_TYPES,
                "view": _string(
                    "latest (restated values) or as_reported (first-reported values).",
                    enum=["latest", "as_reported"],
                ),
                "as_of": _string("Only values known at this UTC time, e.g. 2024-01-01T00:00:00Z."),
                "limit": _integer("Maximum points.", 1, 10_000),
            },
            "required": ["company", "metrics"],
        },
    },
    {
        "name": "list_company_filings",
        "description": "Filings loaded for a company, newest first, with form, filing date, "
        "period end, and accession.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cik": CIK,
                "limit": _integer("Rows per page.", 1, 1_000),
                "offset": _integer("Rows to skip.", 0, 10_000),
            },
            "required": ["cik"],
        },
    },
    {
        "name": "get_filing",
        "description": "One filing's summary (fact and concept counts) and dei cover page "
        "(shares outstanding, public float, auditor, securities).",
        "inputSchema": {
            "type": "object",
            "properties": {"accession": ACCESSION},
            "required": ["accession"],
        },
    },
    {
        "name": "get_filing_segments",
        "description": "Segment, product, geography, and consolidation breakdown facts of a filing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "accession": ACCESSION,
                "kind": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["segment", "product", "geography", "consolidation"],
                    },
                },
                "concept": _string("Concept qname, e.g. us-gaap:Revenues."),
                "include_eliminations": {"type": "boolean"},
                "limit": _integer("Rows per page.", 1, 10_000),
                "offset": _integer("Rows to skip.", 0, 10_000),
            },
            "required": ["accession"],
        },
    },
    {
        "name": "get_filing_text_blocks",
        "description": "Footnote and accounting-policy text blocks of a filing. List them first, "
        "then fetch one with include_text and its concept; text is HTML as filed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "accession": ACCESSION,
                "policy": {"type": "boolean", "description": "True for accounting policies only."},
                "concept": _string("Concept qname of one text block."),
                "include_text": {"type": "boolean"},
                "limit": _integer("Rows per page (at most 100 with include_text).", 1, 1_000),
                "offset": _integer("Rows to skip.", 0, 10_000),
            },
            "required": ["accession"],
        },
    },
    {
        "name": "query_raw_facts",
        "description": "As-filed XBRL facts. Needs a cik, an accession, or a filed date window of "
        "at most 366 days. Filter by concept_name (e.g. Revenues) to keep results small.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cik": CIK,
                "accession": ACCESSION,
                "concept_name": _string("Concept local name, e.g. Revenues."),
                "concept_namespace": _string("Concept namespace URI."),
                "filed_from": _string("YYYY-MM-DD."),
                "filed_to": _string("YYYY-MM-DD."),
                "limit": _integer("Rows per page.", 1, 10_000),
                "offset": _integer("Rows to skip.", 0, 10_000),
            },
        },
    },
    {
        "name": "get_canonical_facts",
        "description": "Every canonical fact mapped to one metric for a company, with lineage to "
        "the raw fact and mapping rule (finer grained than get_financial_series).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cik": CIK,
                "metric": _string("Metric code from list_metrics."),
                "filed_from": _string("YYYY-MM-DD."),
                "filed_to": _string("YYYY-MM-DD."),
                "include_dimensions": {"type": "boolean"},
                "limit": _integer("Rows per page.", 1, 10_000),
                "offset": _integer("Rows to skip.", 0, 10_000),
            },
            "required": ["cik", "metric"],
        },
    },
]


class ToolError(Exception):
    """A tool call that failed in a way the agent should see and can correct."""


class WarehouseTools:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.handlers: dict[str, Callable[[Params], Any]] = {
            "search_companies": self.search_companies,
            "list_metrics": lambda _: self._get("/v1/metrics"),
            "get_financial_series": self.get_financial_series,
            "list_company_filings": self.list_company_filings,
            "get_filing": self.get_filing,
            "get_filing_segments": self.get_filing_segments,
            "get_filing_text_blocks": self.get_filing_text_blocks,
            "query_raw_facts": lambda args: self._get("/v1/facts", _pick(args, *_FACT_KEYS)),
            "get_canonical_facts": self.get_canonical_facts,
        }

    def call(self, name: str, arguments: Params) -> Any:
        handler = self.handlers.get(name)
        if handler is None:
            raise ToolError(f"Unknown tool: {name}")
        return handler(arguments)

    def search_companies(self, args: Params) -> Any:
        return self._get("/v1/companies", {"q": _required(args, "query")} | _pick(args, "limit"))

    def get_financial_series(self, args: Params) -> Any:
        company = str(_required(args, "company")).strip()
        metrics = args.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            raise ToolError("metrics must be a non-empty list of metric codes")
        params: Params = {"metrics": ",".join(str(m) for m in metrics)}
        params |= _pick(args, "view", "as_of", "limit")
        if args.get("period_types"):
            params["period_type"] = list(args["period_types"])
        if company.isdigit():
            return self._get(f"/v1/series/{int(company)}", params)
        return self._get("/v1/series", params | {"ticker": company})

    def list_company_filings(self, args: Params) -> Any:
        cik = _cik(args)
        return self._get(f"/v1/companies/{cik}/filings", _pick(args, "limit", "offset"))

    def get_filing(self, args: Params) -> Any:
        accession = _accession(args)
        summary = self._get(f"/v1/filings/{accession}")
        try:
            cover = self._get(f"/v1/filings/{accession}/cover-page")
        except ToolError:
            cover = None
        return {"summary": summary, "cover_page": cover}

    def get_filing_segments(self, args: Params) -> Any:
        params = _pick(args, "kind", "concept", "include_eliminations", "limit", "offset")
        return self._get(f"/v1/filings/{_accession(args)}/segments", params)

    def get_filing_text_blocks(self, args: Params) -> Any:
        params = _pick(args, "policy", "concept", "include_text", "limit", "offset")
        return self._get(f"/v1/filings/{_accession(args)}/text-blocks", params)

    def get_canonical_facts(self, args: Params) -> Any:
        metric = str(_required(args, "metric"))
        params = _pick(args, "filed_from", "filed_to", "include_dimensions", "limit", "offset")
        return self._get(f"/v1/facts/canonical/{_cik(args)}/{metric}", params)

    def _get(self, path: str, params: Params | None = None) -> Any:
        query = {
            key: str(value).lower() if isinstance(value, bool) else value
            for key, value in (params or {}).items()
        }
        try:
            response = self.client.get(path, params=query)
        except httpx.HTTPError as exc:
            raise ToolError(f"Warehouse API unreachable at {self.client.base_url}: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ToolError(f"API returned {response.status_code}: {detail}")
        return response.json()


_FACT_KEYS = (
    "cik",
    "accession",
    "concept_name",
    "concept_namespace",
    "filed_from",
    "filed_to",
    "limit",
    "offset",
)


def _pick(args: Params, *keys: str) -> Params:
    return {key: args[key] for key in keys if args.get(key) is not None}


def _required(args: Params, key: str) -> Any:
    value = args.get(key)
    if value is None or value == "":
        raise ToolError(f"{key} is required")
    return value


def _cik(args: Params) -> int:
    value = _required(args, "cik")
    try:
        cik = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError("cik must be an integer") from exc
    if cik < 0:
        raise ToolError("cik must not be negative")
    return cik


def _accession(args: Params) -> str:
    accession = str(_required(args, "accession"))
    # The API validates the full pattern; this only keeps the value one path segment.
    if "/" in accession or "?" in accession or "#" in accession or ".." in accession:
        raise ToolError("Invalid accession")
    return accession


def _tool_result(payload: Any, *, error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = (
            text[:MAX_RESULT_CHARS]
            + f"\n... truncated at {MAX_RESULT_CHARS} characters; request a smaller page."
        )
    return {"content": [{"type": "text", "text": text}], "isError": error}


def handle(tools: WarehouseTools, message: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; notifications (no id) get no response."""
    method = message.get("method")
    if "id" not in message:
        return None
    request_id = message["id"]
    params = message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        result: Any = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sec-xbrl-warehouse", "version": __version__},
            "instructions": "Read-only access to XBRL financial data. Start with search_companies "
            "to find a CIK, list_metrics for metric codes, then get_financial_series.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            result = _tool_result(tools.call(params.get("name", ""), params.get("arguments") or {}))
        except ToolError as exc:
            result = _tool_result(str(exc), error=True)
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(tools: WarehouseTools, stdin: IO[str], stdout: IO[str]) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            response = handle(tools, message) if isinstance(message, dict) else None
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


def main() -> None:
    base_url = os.environ.get("XBRL_MCP_API_URL", "http://127.0.0.1:8080")
    headers = {}
    if api_key := os.environ.get("XBRL_API_KEY", "").strip():
        headers["Authorization"] = f"Bearer {api_key}"
    with httpx.Client(base_url=base_url, headers=headers, timeout=60) as client:
        serve(WarehouseTools(client), sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
