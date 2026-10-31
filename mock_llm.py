"""Deterministic OpenAI-compatible stub for astrmai tests.

Planner requests get a `reply` tool call carrying the msg_id lifted from the prompt; task
requests without tools get plain speech text. Every request is appended to requests.jsonl so
tests can assert on the wire instead of guessing.
"""

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 18099
LOG = "mock_llm_requests.jsonl"
MSG_ID_RE = re.compile(r'msg_id="([A-Za-z0-9_-]+)"')

_lock = threading.Lock()


def _log(record: dict) -> None:
    with _lock:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _flatten(messages: list) -> str:
    parts = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(seg.get("text", "")) for seg in content if isinstance(seg, dict))
    return "\n".join(parts)


def respond(payload: dict) -> dict:
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    names = [(t.get("function") or {}).get("name") for t in tools]
    blob = _flatten(messages)

    if "reply" in names:
        msg_ids = MSG_ID_RE.findall(blob)
        args = {
            "msg_id": msg_ids[-1] if msg_ids else "",
            "reply_reference": "mock：用一句话回应",
            "reply_style": "简短回复",
            "attach_pic": [],
        }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"mockcall{len(msg_ids)}",
                    "type": "function",
                    "function": {"name": "reply", "arguments": json.dumps(args, ensure_ascii=False)},
                }
            ],
        }, "tool_calls"

    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    if isinstance(last_user, list):
        last_user = " ".join(str(seg.get("text", "")) for seg in last_user if isinstance(seg, dict))
    text = f"【mock】收到：{str(last_user).strip()[:40]}" if not tools else "【mock】无需工具"
    return {"role": "assistant", "content": text}, "message"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, body: dict, code: int = 200) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json({"object": "list", "data": [{"id": "mock-llm", "object": "model", "owned_by": "astrmai"}]})
        elif self.path == "/":
            # 有人会在浏览器里打开这个端口，给一句人话比 404 好
            raw = b"astrmai test stub - not a web panel. Use POST /v1/chat/completions\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self._json({"error": "not found"}, 404)

    def do_HEAD(self) -> None:  # noqa: N802
        # BaseHTTPRequestHandler 默认对未实现的方法回 501，浏览器预检会撞上它
        code = 200 if self.path in ("/", "/v1/models") or self.path.rstrip("/").endswith("/models") else 404
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._json({"error": f"bad json: {exc}"}, 400)
            return

        message, finish = respond(payload)
        completion = {
            "id": "mock-completion",
            "object": "chat.completion",
            "model": payload.get("model", "mock-llm"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        _log(
            {
                "path": self.path,
                "model": payload.get("model"),
                "tools": [(t.get("function") or {}).get("name") for t in payload.get("tools") or []],
                "n_messages": len(payload.get("messages") or []),
                "finish": finish,
                "message": message,
            }
        )

        if payload.get("stream"):
            chunk = {
                "id": completion["id"],
                "object": "chat.completion.chunk",
                "model": completion["model"],
                "choices": [{"index": 0, "delta": message, "finish_reason": finish}],
            }
            raw = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self._json(completion)

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"mock llm on http://{HOST}:{PORT}/v1  log={LOG}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
