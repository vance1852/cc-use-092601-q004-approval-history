"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import ServiceError
from .service import PhotonService


def _parts(path: str) -> list[str]:
    return [part for part in urlparse(path).path.split("/") if part]


def _error(exc: Exception) -> tuple[int, dict]:
    if isinstance(exc, ServiceError):
        return exc.status, {"error": {"code": exc.code, "message": str(exc)}}
    if isinstance(exc, PermissionError):
        return 403, {"error": {"code": "forbidden", "message": str(exc)}}
    if isinstance(exc, KeyError):
        return 404, {"error": {"code": "not_found", "message": str(exc)}}
    return 400, {"error": {"code": "bad_request", "message": str(exc)}}


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()
    # 多个请求线程共用一个 SQLite 连接，串行化请求避免游标并发使用。
    db_lock = threading.RLock()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    # GET /health
    # GET /lots/{lot_id}
    # GET /lots/{lot_id}/analyses
    # GET /lots/{lot_id}/reviews
    # GET /lots/{lot_id}/approvals            当前有效决定 + 完整决定链
    def _handle_get(self, parts: list[str]) -> tuple[int, dict]:
        token = self._token()
        if self.path.startswith("/health"):
            return 200, {"status": "ok", "service": "photon-fab"}
        if len(parts) == 2 and parts[0] == "lots":
            return 200, self.service.get_lot(token, parts[1])
        if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analyses":
            return 200, {"analyses": self.service.list_analyses(token, parts[1])}
        if len(parts) == 3 and parts[0] == "lots" and parts[2] == "reviews":
            return 200, {"reviews": self.service.list_reviews(token, parts[1])}
        if len(parts) == 3 and parts[0] == "lots" and parts[2] == "approvals":
            return 200, self.service.decision_report(token, parts[1])
        return 404, {"error": {"code": "not_found", "message": "not found"}}

    # POST /login
    # POST /lots
    # POST /lots/{lot_id}/measurements
    # POST /lots/{lot_id}/analysis
    # POST /lots/{lot_id}/reviews              显式发起复议
    # POST /lots/{lot_id}/approvals            追加不可变决定（支持 Idempotency-Key）
    def _handle_post(self, parts: list[str], body: dict) -> tuple[int, dict]:
        if len(parts) == 1 and parts[0] == "login":
            return 200, {"token": self.service.auth.login(body["user_id"], body["password"])}
        token = self._token()
        if len(parts) == 1 and parts[0] == "lots":
            return 201, self.service.create_lot(
                token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"])
        if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
            return 201, self.service.add_measurement(
                token, parts[1], body["wavelength_nm"], body["response"],
                body.get("noise", 0.0), body["instrument"])
        if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
            return 200, self.service.analyze(token, parts[1])
        if len(parts) == 3 and parts[0] == "lots" and parts[2] == "reviews":
            result = self.service.request_review(
                token, parts[1], body["reason"],
                None if body.get("expected_revision") is None else int(body["expected_revision"]),
            )
            return 201, result
        if len(parts) == 3 and parts[0] == "lots" and parts[2] == "approvals":
            result = self.service.approve(
                token, parts[1], body["decision"], body["reason"],
                analysis_id=body.get("analysis_id"),
                review_id=None if body.get("review_id") is None else int(body["review_id"]),
                idempotency_key=self.headers.get("Idempotency-Key"),
                expected_revision=None if body.get("expected_revision") is None
                else int(body["expected_revision"]),
            )
            return (200 if result.get("replayed") else 201), result
        return 404, {"error": {"code": "not_found", "message": "not found"}}

    def do_GET(self):
        parts = _parts(self.path)
        try:
            with self.db_lock:
                status, body = self._handle_get(parts)
        except Exception as exc:
            status, body = _error(exc)
        self._json(status, body)

    def do_POST(self):
        parts = _parts(self.path)
        try:
            body = self._body()
            with self.db_lock:
                status, response_body = self._handle_post(parts, body)
        except Exception as exc:
            status, response_body = _error(exc)
        self._json(status, response_body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = PhotonService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
