"""A tiny OpenAI-compatible server that plays a fixed script of tool calls.

Plugin tools are deferred behind Hermes's tool_search bridge, so the script calls
them through ``tool_call`` exactly as a real model would. It stands in for a model so the demo is reproducible and needs no API key: Hermes
runs its real agent loop, tool registry, plugin hooks and approval gate against it.
The "model" deliberately tries to overspend and to retry, to show Guardrails holding.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

STEPS = [
    ("guardrails_status", {}),
    ("guardrails_pay", {"payee": "cloud-gpu-rentals", "amount": "75.00", "memo": "GPU hours"}),
    ("guardrails_pay", {"payee": "acme-hosting", "amount": "12.00", "memo": "VPS for November", "idempotency_key": "order-1001"}),
    ("guardrails_pay", {"payee": "acme-hosting", "amount": "12.00", "memo": "VPS for November (retry)", "idempotency_key": "order-1001"}),
    ("guardrails_audit", {"limit": 10}),
]
FINAL = "Done. One payment of 12.00 USD to acme-hosting went through, the 75.00 USD GPU charge was refused by policy, and the retry was recognised as a duplicate, so nothing was charged twice."


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "scripted", "object": "model"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        msgs = req.get("messages", [])
        done = sum(1 for m in msgs if m.get("role") == "tool")
        if done < len(STEPS):
            name, args = STEPS[done]
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{done}", "type": "function", "function": {"name": "tool_call", "arguments": json.dumps({"name": name, "arguments": args})}}]}
            finish = "tool_calls"
        else:
            msg, finish = {"role": "assistant", "content": FINAL}, "stop"
        out = {"id": f"x{time.time_ns()}", "object": "chat.completion", "created": int(time.time()), "model": "scripted",
               "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = dict(msg)
            if "tool_calls" in delta:
                delta["tool_calls"] = [{**delta["tool_calls"][0], "index": 0}]
            for chunk in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": out["usage"]}):
                chunk.update(id=out["id"], object="chat.completion.chunk", created=out["created"], model="scripted")
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._send(out)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8765), H).serve_forever()
