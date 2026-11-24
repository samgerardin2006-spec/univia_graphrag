from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .generate import recommend
from .retrieve import retrieve
from .schema import ValidationError
from .questions import answer_question


def serve(database, port=8000):
    if not Path(database).is_file():
        raise ValidationError("Database is missing; run ingest first")

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, content, content_type="application/json; charset=utf-8"):
            body = content.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self.send(200, Path(__file__).with_name("index.html").read_text(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                self.send(200, json.dumps({"mistral_configured": bool(os.environ.get("MISTRAL_API_KEY")), "model": os.environ.get("MISTRAL_MODEL") or "mistral-small-latest", "key_status": "loaded_at_startup" if os.environ.get("MISTRAL_API_KEY") else "missing", "server_version": "question-api-v2"}))
            else:
                self.send(404, '{"error":"Not found"}')

        def do_POST(self):
            if self.path not in ("/api/retrieve", "/api/recommend", "/api/ask"):
                self.send(404, '{"error":"Not found"}')
                return
            # Local UI only: reject browser cross-origin requests and DNS rebinding.
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}") or self.headers.get("Origin") not in (None, f"http://{host}"):
                self.send(403, '{"error":"Use the local app origin"}')
                return
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValidationError("Content-Type must be application/json")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100_000:
                    raise ValidationError("Request must be between 1 and 100000 bytes")
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/ask":
                    if not isinstance(body, dict) or set(body) - {"question", "offline"} or type(body.get("offline", False)) is not bool:
                        raise ValidationError("Expected a question and optional offline boolean")
                    result = answer_question(database, body.get("question"), offline=body.get("offline", False))
                    api = result.get("api", {})
                    print(f"Mistral: status={api.get('status')} attempted={api.get('called')} model={api.get('model')} request_id={api.get('request_id', 'none')}", flush=True)
                    self.send(200, json.dumps(result, ensure_ascii=False))
                    return
                if not isinstance(body, dict) or set(body) - {"profile", "top_k", "offline"}:
                    raise ValidationError("Expected profile, optional top_k and offline")
                if type(body.get("offline", False)) is not bool:
                    raise ValidationError("offline must be boolean")
                result = retrieve(database, body.get("profile"), body.get("top_k", 8))
                if self.path == "/api/recommend":
                    result = recommend(result, offline=body.get("offline", False))
                self.send(200, json.dumps(result, ensure_ascii=False))
            except (ValidationError, ValueError, OSError) as exc:
                self.send(400, json.dumps({"error": str(exc)}))

    print(f"UNIVIA: http://127.0.0.1:{port} (fictional profiles only)", flush=True)
    print("Mistral API key: " + ("loaded (not yet tested)" if os.environ.get("MISTRAL_API_KEY") else "not loaded; database preview is available"), flush=True)
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
