"""Native HeavyIQ /completions task protocol -> local instruction model chat.

Streaming is buffered: one complete upstream answer is emitted as SSE. This
preserves HeavyIQ's wire contract without claiming token-streaming latency.
"""
import json
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
TASKS = {
    "sql": "Return only one read-only HeavySQL SELECT query. No markdown or explanation.",
    "sql error": "Correct the failed query. Return only one read-only HeavySQL SELECT query.",
    "english": "Explain the supplied SQL results faithfully. Do not invent evidence.",
    "table": "Follow the requested table-selection output format exactly.",
    "table question": "Follow the requested question-list output format exactly.",
    "": "Follow the instructions and output format in the task.",
}


def messages(prompt, stop=None):
    match = re.match(r"<\|(?:(.*?) )?prompt\|>\n", prompt)
    if match and (match.group(1) or "") in TASKS:
        task = match.group(1) or ""
        body = prompt[match.end():]
    else:
        stops = [stop] if isinstance(stop, str) else (stop or [])
        hints = {hint.group(1) or "" for value in stops
                 if isinstance(value, str) and (hint := re.fullmatch(r"\n<\|(?:(.*?) )?answer\|>", value))
                 and (hint.group(1) or "") in TASKS}
        if len(hints) != 1 or re.match(r"<\|", prompt):
            raise ValueError("Unsupported HeavyIQ task marker")
        task = hints.pop()
        body = prompt
    body = re.sub(r"\n<\|(?:.*? )?answer\|>\n?$", "", body)
    system = ("You are a HEAVY.AI HeavySQL assistant. " + TASKS[task] + "\n"
              "Use DATEADD('hour', -24, CURRENT_TIMESTAMP) for recent windows; "
              "DATE_TRUNC('day', acquired_at) for UTC days. No PostgreSQL INTERVAL, "
              "PostGIS constructors, or invented columns. Respect the provided schema.\n"
              "Answer the exact question. Never add a grouping or satellite breakdown unless requested. "
              "A request for a count without a breakdown needs one total count column and no GROUP BY.\n"
              "When no time range is requested, include all available dates: do not add a recent-time filter. "
              "For UTC day grouping, use DATE_TRUNC('day', acquired_at).\n"
              "Examples below illustrate syntax only; never use an example table absent from the task schema.\n"
              + (ROOT / "sql/heavyiq-guidance.txt").read_text()
              + "\nExamples:\n" + (ROOT / "sql/heavysql-examples.txt").read_text())
    return task, [{"role": "system", "content": system}, {"role": "user", "content": body}]


def clean(text, task):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if task in {"sql", "sql error"}:
        text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text, flags=re.I).strip()
    return text


class Handler(BaseHTTPRequestHandler):
    def json_reply(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            return self.json_reply({"object": "list", "data": [{"id": "firms-nemotron",
                "object": "model", "max_model_len": 6000, "owned_by": "local"}]})
        self.json_reply({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path in {"/v1/tokenize", "/v1/detokenize"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 131072:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                endpoint = self.path.removeprefix("/v1")
                upstream = Request("http://127.0.0.1:8101" + endpoint,
                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                with urlopen(upstream, timeout=30) as response:
                    return self.json_reply(json.load(response))
            except (ValueError, TypeError):
                return self.json_reply({"error": "Invalid tokenizer request"}, 400)
            except Exception:
                return self.json_reply({"error": "Local tokenizer unavailable"}, 503)
        if self.path != "/v1/completions":
            return self.json_reply({"error": "Not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 131072:
                raise ValueError("Invalid request size")
            request = json.loads(self.rfile.read(length))
            if request.get("logprobs") or request.get("use_beam_search"):
                raise ValueError("Scoring/beam search unsupported")
            prompt = request["prompt"]
            if isinstance(prompt, list):
                if len(prompt) != 1:
                    raise ValueError("Only one prompt per request")
                prompt = prompt[0]
            task, chat = messages(prompt, stop=request.get("stop"))
            n = int(request.get("n") or 1)
            if not 1 <= n <= 5:
                raise ValueError("Unsupported candidate count")
            choices = []
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            for index in range(n):
                payload = {"model": "firms-nemotron", "messages": chat, "stream": False,
                           "max_tokens": min(1536, int(request.get("max_tokens") or 768)),
                           "temperature": float(request.get("temperature") or 0),
                           "seed": int(request.get("seed") if request.get("seed") is not None else 42) + index,
                           "chat_template_kwargs": {"enable_thinking": False}}
                if request.get("stop"):
                    payload["stop"] = request["stop"]
                upstream = Request("http://127.0.0.1:8101/v1/chat/completions",
                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                with urlopen(upstream, timeout=300) as response:
                    result = json.load(response)
                choice = result["choices"][0]
                choices.append({"index": index, "text": clean(choice["message"]["content"], task),
                                "logprobs": None, "finish_reason": choice.get("finish_reason", "stop")})
                for key in usage:
                    usage[key] += result.get("usage", {}).get(key, 0)
            output = {"id": "cmpl-" + uuid.uuid4().hex, "created": int(time.time()),
                      "model": "firms-nemotron", "object": "text_completion",
                      "choices": choices, "usage": usage}
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(("data: " + json.dumps(output) + "\n\ndata: [DONE]\n\n").encode())
            else:
                self.json_reply(output)
        except (ValueError, KeyError, TypeError) as exc:
            prompt_value = locals().get("prompt")
            print(json.dumps({"event": "invalid_completion_request",
                "error_type": type(exc).__name__,
                "reason": str(exc) if str(exc) in {"Unsupported HeavyIQ task marker",
                    "Only one prompt per request", "Unsupported candidate count",
                    "Scoring/beam search unsupported", "Invalid request size"} else "Invalid field value",
                "prompt_type": type(prompt_value).__name__,
                "task_marker": re.match(r"\s*(<\|[a-z ]+\|>)", prompt_value).group(1)
                    if isinstance(prompt_value, str) and re.match(r"\s*(<\|[a-z ]+\|>)", prompt_value) else None,
                "prompt_element_type": type(prompt_value[0]).__name__
                    if isinstance(prompt_value, list) and prompt_value else None}), flush=True)
            self.json_reply({"error": "Invalid or unsupported completion request"}, 400)
        except Exception:
            self.json_reply({"error": "Local model unavailable"}, 503)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    # Reachable from the HeavyAI container, not via EC2 inbound ports.
    ThreadingHTTPServer(("172.17.0.1", 8100), Handler).serve_forever()
