"""create() with two LLM columns against a local stub endpoint (0.5 s per request)."""

import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import data_designer.config as dd
from data_designer.interface import DataDesigner


class Slow(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        time.sleep(0.5)
        body = json.dumps(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "stub-model",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


with ThreadingHTTPServer(("127.0.0.1", 0), Slow) as server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    provider = dd.ModelProvider(name="local", endpoint=f"http://127.0.0.1:{server.server_address[1]}/v1", api_key="k")
    b = dd.DataDesignerConfigBuilder(
        model_configs=[dd.ModelConfig(alias="stub", model="stub-model", provider="local", skip_health_check=True)]
    )
    b.add_column(
        dd.SamplerColumnConfig(name="topic", sampler_type="category", params=dd.CategorySamplerParams(values=["a", "b"]))
    )
    b.add_column(dd.LLMTextColumnConfig(name="question", prompt="Ask about {{ topic }}", model_alias="stub"))
    b.add_column(dd.LLMTextColumnConfig(name="answer", prompt="Answer {{ question }}", model_alias="stub"))
    with tempfile.TemporaryDirectory() as d:
        DataDesigner(artifact_path=d, model_providers=[provider]).create(b, num_records=8)
    server.shutdown()
