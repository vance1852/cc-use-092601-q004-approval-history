"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ServiceError
from .service import PhotonService


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()
    # 多线程 HTTP 服务复用同一个 SQLite 连接；用锁把每个业务用例串行化，
    # 避免同一事务内的多条语句被不同请求线程交错执行。
    service_lock = threading.RLock()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def _idempotency_key(self, body: dict) -> str:
        return (self.headers.get("Idempotency-Key") or body.get("idempotency_key") or "").strip()

    def do_GET(self):
        with self.service_lock:
            return self._handle_get()

    def _handle_get(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "photon-fab"})
        if self.path.startswith("/lots/"):
            try:
                token = self._token()
                parts = self.path.split("/")
                lot_id = parts[2]
                if len(parts) == 3:
                    return self._json(200, self.service.get_lot(token, lot_id))
                if len(parts) == 4 and parts[3] == "report":
                    return self._json(200, self.service.report(token, lot_id))
                if len(parts) == 4 and parts[3] == "decisions":
                    return self._json(200, {"decisions": self.service.decision_chain(token, lot_id)})
                return self._json(404, {"error": "not found"})
            except ServiceError as exc:
                return self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
            except PermissionError as exc:
                return self._json(403, {"error": {"code": "forbidden", "message": str(exc)}})
            except KeyError as exc:
                return self._json(404, {"error": {"code": "not_found", "message": str(exc)}})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        with self.service_lock:
            return self._handle_post()

    def _handle_post(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if self.path.startswith("/lots/"):
                parts = self.path.split("/")
                lot_id = parts[2]
                action = parts[3] if len(parts) == 4 else ""
                if action == "measurements":
                    return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
                if action == "analysis":
                    return self._json(200, self.service.analyze(token, lot_id))
                if action == "submit":
                    return self._json(200, self.service.submit_for_review(token, lot_id, body.get("expected_revision")))
                if action == "review-requests":
                    return self._json(201, self.service.request_review(token, lot_id, body["reason"], self._idempotency_key(body), body.get("expected_revision")))
                if action == "decisions":
                    return self._json(201, self.service.decide(token, lot_id, body["decision"], body["reason"], body["analysis_id"], self._idempotency_key(body), body.get("expected_revision"), body.get("review_request_id")))
            return self._json(404, {"error": "not found"})
        except ServiceError as exc:
            return self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except PermissionError as exc:
            return self._json(403, {"error": {"code": "forbidden", "message": str(exc)}})
        except KeyError as exc:
            return self._json(404, {"error": {"code": "not_found", "message": str(exc)}})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})


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
