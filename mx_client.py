"""Call the session's 东方财富妙想 MCP connector (via WorkBuddy local connector proxy)."""
import json, os, urllib.request

def _cfg():
    cfg = json.loads(os.environ["CODEBUDDY_MCP_CONFIG"])["mcpServers"]["mx-ds-mcp"]
    return cfg["url"], cfg.get("headers", {})

def _post(url, headers, payload, sid=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers}
    if sid: h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=180) as r:
        sid = r.headers.get("Mcp-Session-Id") or sid
        body = r.read().decode()
    if body.lstrip().startswith("event:") or "data:" in body[:20]:
        msgs = [json.loads(l[5:]) for l in body.splitlines() if l.startswith("data:")]
        body = msgs[-1] if msgs else {}
    else:
        body = json.loads(body) if body.strip() else {}
    return body, sid

_session = {}
def call(tool, query):
    url, headers = _cfg()
    sid = _session.get("sid")
    if not sid:
        _, sid = _post(url, headers, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "etf-board", "version": "1"}}})
        _post(url, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        _session["sid"] = sid
    res, _ = _post(url, headers, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": tool, "arguments": {"query": query}}}, sid)
    if "error" in res: raise RuntimeError(res["error"])
    text = "".join(c.get("text", "") for c in res["result"]["content"])
    return json.loads(text)

if __name__ == "__main__":
    import sys
    print(json.dumps(call("mx_fund_finance_data", sys.argv[1]), ensure_ascii=False)[:1500])
