"""Standard HTTP server that always responds with the text "okay"."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8000
BODY = b"okay"


class OkayHandler(BaseHTTPRequestHandler):
    def _respond(self, send_body: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        if send_body:
            self.wfile.write(BODY)

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def do_HEAD(self):
        self._respond(send_body=False)


if __name__ == "__main__":
    with ThreadingHTTPServer((HOST, PORT), OkayHandler) as httpd:
        print(f"Serving on http://{HOST}:{PORT}", flush=True)
        httpd.serve_forever()
