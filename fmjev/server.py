import argparse
import math
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import APIError, DecisionService, FMBackend, MODEL, dumps, loads

MAX_BODY = 256 * 1024


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FmJev/0.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            pass  # No user data, paths or headers in access logs.

        def respond(self, status, body):
            data = dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if status == 429:
                self.send_header("Retry-After", "2")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self.respond(200, {"status": "ok", "model": MODEL, "backend_ready": "unchecked"})
            else:
                self.respond(404, {"error": {"code": "not_found", "message": "Unknown endpoint."}})

        def do_POST(self):
            try:
                if self.path != "/v1/systemone":
                    raise APIError(404, "not_found", "Unknown endpoint.")
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    raise APIError(415, "unsupported_media_type", "Use Content-Type: application/json.")
                if self.headers.get("Transfer-Encoding"):
                    raise APIError(400, "invalid_request", "Chunked requests are not supported.")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1:
                    raise APIError(411, "length_required", "A single Content-Length is required.")
                try:
                    length = int(lengths[0])
                except ValueError:
                    raise APIError(400, "invalid_request", "Invalid Content-Length.") from None
                if length < 0 or length > MAX_BODY:
                    raise APIError(413, "body_too_large", "Request body must be at most 256 KiB.")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise APIError(400, "invalid_request", "Incomplete request body.")
                try:
                    body = loads(raw.decode("utf-8"))
                except (ValueError, RecursionError):
                    raise APIError(400, "invalid_json", "Body must be valid UTF-8 JSON with unique keys and finite numbers.") from None
                self.respond(200, service.evaluate(body))
            except APIError as error:
                self.respond(error.status, {"error": {"code": error.code, "message": str(error)}})
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                return

    return Handler


def positive_seconds(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("Must be a finite positive number.")
    return value


def main():
    parser = argparse.ArgumentParser(description="FmJev: Jev-style API backed by fm.")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--fm", default="fm", help="fm executable path")
    parser.add_argument("--timeout", type=positive_seconds, default=60, help="Seconds per fm call")
    parser.add_argument("--request-timeout", type=positive_seconds, default=180)
    args = parser.parse_args()
    service = DecisionService(FMBackend(args.fm, args.timeout), args.request_timeout)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(service))
    print(f"FmJev listening on http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
