"""A scripted stand-in for the Anthropic Messages API, enough for Claude Code -p to run a turn.

Each POST /v1/messages is logged. The reply follows `FakeModel.plan`: a list of replies consumed
in order, each either ("text", str) or ("tool", name, input). When the plan is empty it answers
"Done.". Streaming and non-streaming requests are both answered.
"""
from __future__ import annotations

import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModel:
    def __init__(self):
        self.requests: list[dict] = []
        self.plan: list[tuple] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def next_reply(self, body: dict) -> tuple:
        tools = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
        with self.lock:
            # Side requests (titles, summaries) carry no tools; only the main loop gets the plan.
            if self.plan and tools:
                return self.plan.pop(0)
        return ("text", "Done.")


_ids = itertools.count(1)


def _events(reply: tuple, model: str) -> list[dict]:
    usage = {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0}
    out = [{"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": usage}}]
    if reply[0] == "tool":
        out += [{"type": "content_block_start", "index": 0, "content_block": {
                    "type": "tool_use", "id": f"toolu_fake{next(_ids):04}", "name": reply[1],
                    "input": {}}},
                {"type": "content_block_delta", "index": 0, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(reply[2])}},
                {"type": "content_block_stop", "index": 0}]
        stop = "tool_use"
    else:
        out += [{"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": reply[1]}},
                {"type": "content_block_stop", "index": 0}]
        stop = "end_turn"
    out += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"}]
    return out


def _message(reply: tuple, model: str) -> dict:
    if reply[0] == "tool":
        content = [{"type": "tool_use", "id": f"toolu_fake{next(_ids):04}", "name": reply[1],
                    "input": reply[2]}]
        stop = "tool_use"
    else:
        content = [{"type": "text", "text": reply[1]}]
        stop = "end_turn"
    return {"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}}


def _handler(fake: FakeModel):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply_json(self, status, doc):
            data = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply_json(200, {"data": [], "has_more": False})

        do_HEAD = do_GET

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if "count_tokens" in self.path:
                return self.reply_json(200, {"input_tokens": 100})
            if not self.path.startswith("/v1/messages"):
                return self.reply_json(200, {})
            fake.requests.append(body)
            reply = fake.next_reply(body)
            model = body.get("model", "claude-fake")
            if not body.get("stream"):
                return self.reply_json(200, _message(reply, model))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for event in _events(reply, model):
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
            self.close_connection = True

    return Handler
