import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edge.deployments import Gateway
from service_deployments import Plan
from tests.unit.test_service_deployments import plan


class Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"


class Store:
    def __init__(self, p):
        self.p = p

    def read(self):
        return self.p


def app_for(p, handler):
    app = FastAPI()
    gateway = Gateway(
        Store(p),
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw),
    )
    # Request is local because it only annotates this generated test route.
    from fastapi import Request

    @app.api_route("/proxy/{path:path}", methods=["GET", "POST"])
    async def proxy(request: Request, path: str):
        return await gateway.proxy(request, "llm-services", "chat", path)

    return TestClient(app)


def test_request_time_failover_and_primary_recovery_preserve_payload():
    primary_up = False
    seen = []

    def upstream(request):
        seen.append(
            (request.method, request.url.host, request.url.path, request.content)
        )
        if request.url.path == "/health":
            if request.url.host == "kraken" and not primary_up:
                return httpx.Response(503, json={"status": "loading"})
            return httpx.Response(200, json={"model": "same-model"})
        return httpx.Response(
            200, stream=Stream(), headers={"content-type": "text/event-stream"}
        )

    client = app_for(plan("ha", ("kraken", "rainbow")), upstream)
    r = client.post(
        "/proxy/chat/completions",
        content=b'{"stream":true}',
        headers={"Authorization": "Bearer private"},
    )
    assert r.status_code == 200 and r.headers["x-chronicle-instance"] == "rainbow"
    assert r.text == "data: first\n\ndata: [DONE]\n\n"
    assert seen[-1] == ("POST", "rainbow", "/v1/chat/completions", b'{"stream":true}')
    primary_up = True
    assert (
        client.post("/proxy/chat/completions", json={}).headers["x-chronicle-instance"]
        == "kraken"
    )


def test_submitted_post_is_never_replayed_on_timeout():
    posts = []

    def upstream(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"model": "same-model"})
        posts.append(request.url.host)
        raise httpx.ReadTimeout("ambiguous response timeout")

    r = app_for(plan("ha", ("kraken", "rainbow")), upstream).post(
        "/proxy/chat/completions", json={"prompt": "one"}
    )
    assert r.status_code == 502 and posts == ["kraken"]


def test_wrong_model_and_all_failed_return_unavailable():
    def upstream(request):
        return httpx.Response(200, json={"model": "wrong-model"})

    r = app_for(plan("ha", ("kraken", "rainbow")), upstream).post(
        "/proxy/chat/completions", json={}
    )
    assert r.status_code == 503
    assert all(not i["healthy"] for i in r.json()["detail"]["instances"])


def test_single_never_selects_an_unlisted_fallback():
    hosts = []

    def upstream(request):
        hosts.append(request.url.host)
        return httpx.Response(503)

    assert (
        app_for(plan(), upstream).post("/proxy/chat/completions", json={}).status_code
        == 503
    )
    assert hosts == ["rainbow"]


def test_failover_over_real_http_connections():
    """Exercise actual HTTPX socket/stream behavior against two warm replicas."""
    # Socket-server imports are local because only this integration test needs them.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from fastapi import Request

    available = [True, True]
    posts = []
    servers = []
    for index in range(2):

        def handler_for(index):
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    payload = json.dumps({"model": "same-model"}).encode()
                    self.send_response(200 if available[index] else 503)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def do_POST(self):
                    if self.headers.get("Transfer-Encoding") == "chunked":
                        parts = []
                        while True:
                            size = int(self.rfile.readline().strip().split(b";")[0], 16)
                            if size == 0:
                                self.rfile.readline()
                                break
                            parts.append(self.rfile.read(size))
                            self.rfile.read(2)
                        body = b"".join(parts)
                    else:
                        body = self.rfile.read(int(self.headers["Content-Length"]))
                    posts.append((index, body))
                    payload = b"data: first\n\ndata: [DONE]\n\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, *args):
                    pass

            return Handler

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(index))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    try:
        p = plan("ha", ("kraken", "rainbow")).model_dump()
        for instance, server in zip(
            p["deployments"]["llm-services"]["instances"], servers
        ):
            endpoint = instance["endpoints"]["chat"]
            base = f"http://127.0.0.1:{server.server_port}"
            endpoint.update(
                url=base + "/v1",
                health_url=base + "/health",
                identity_url=base + "/models",
            )
        gateway = Gateway(Store(Plan.model_validate(p)))
        app = FastAPI()

        @app.post("/chat")
        async def chat(request: Request):
            return await gateway.proxy(
                request, "llm-services", "chat", "chat/completions"
            )

        with TestClient(app) as client:
            for primary_up, expected in [
                (True, "kraken"),
                (False, "rainbow"),
                (True, "kraken"),
            ]:
                available[0] = primary_up
                response = client.post("/chat", json={"stream": True})
                assert response.status_code == 200
                assert response.headers["x-chronicle-instance"] == expected
                assert response.text.endswith("data: [DONE]\n\n")
        assert [index for index, _ in posts] == [0, 1, 0]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
