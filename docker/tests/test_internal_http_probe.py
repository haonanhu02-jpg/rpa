"""Exercise the exact Python checker executed inside the OpenAPI container."""

import importlib.util
import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

DOCKER = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("http_probe", DOCKER / "scripts/http-deploy.py")
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


class InternalHttpProbeTest(unittest.TestCase):
    def run_checker(self, status=400, body=None):
        calls = []

        class Opener:
            def open(self, request, timeout):
                url = request.full_url
                calls.append(url)
                if "resource-service" in url:
                    payload = body if body is not None else {"code": "500001", "message": "fileId"}
                    raise urllib.error.HTTPError(url, status, "fixture", {}, io.BytesIO(json.dumps(payload).encode()))
                payload = {"ready": True} if "health/ready" in url else {"message": "ok"}
                response = io.BytesIO(json.dumps(payload).encode())
                response.code = 200
                return response

        output = io.StringIO()
        with patch("urllib.request.build_opener", return_value=Opener()), patch("sys.stdout", output):
            exec(DEPLOY.INTERNAL_CHECK, {})  # noqa: S102 - execute the checked-in container probe verbatim
        return calls, output.getvalue()

    def test_missing_file_id_response_proves_resource_controller_is_serving(self):
        calls, output = self.run_checker()
        self.assertIn("http://resource-service:8030/api/resource/file/download", calls)
        self.assertNotIn("http://resource-service:8030/api/resource/", calls)
        self.assertIn("内部服务检查通过", output)

    def test_server_errors_are_never_treated_as_ready(self):
        with self.assertRaises(SystemExit) as result:
            self.run_checker(500, {"code": "900004"})
        self.assertEqual(result.exception.code, 1)

    def test_unrelated_bad_request_does_not_pass(self):
        with self.assertRaises(SystemExit):
            self.run_checker(400, {"code": "900004"})


if __name__ == "__main__":
    unittest.main()
