"""Check image dependency snapshots against uv.lock and bundled wheel hashes."""

import hashlib
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PythonRuntimeLockTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("uv"), "uv unavailable")
    def test_committed_runtime_requirements_match_exported_uv_locks(self):
        for service in ("ai-service", "openapi-service"):
            with self.subTest(service=service):
                project = ROOT / "backend" / service
                result = subprocess.run(
                    ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements.txt"],
                    cwd=project,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0)

                def body(content):
                    return "\n".join(line for line in content.splitlines() if not line.startswith("#"))

                self.assertEqual(body(result.stdout), body((project / "requirements.lock").read_text(encoding="utf-8")))

    def test_local_openapi_wheel_is_present_with_its_actual_hash(self):
        project = ROOT / "backend/openapi-service"
        wheel = project / "app/utils/rpawebsocket-1.0.7-py3-none-any.whl"
        requirements = (project / "requirements.lock").read_text(encoding="utf-8")
        self.assertIn("./app/utils/" + wheel.name, requirements)
        self.assertIn("--hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(), requirements)

    def test_python_images_install_pinned_hashed_dependencies_with_index_fallback(self):
        for service in ("ai-service", "openapi-service"):
            with self.subTest(service=service):
                project = ROOT / "backend" / service
                dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
                self.assertNotIn("pip install --no-cache-dir -e .", dockerfile)
                self.assertIn("--require-hashes", dockerfile)
                self.assertIn('--index-url "$PYPI_INDEX_URL"', dockerfile)
                self.assertIn('--index-url "$PYPI_FALLBACK_INDEX_URL"', dockerfile)
                requirements = (project / "requirements.lock").read_text(encoding="utf-8")
                self.assertRegex(requirements, re.compile(r"^aiomysql==0\.2\.0", re.MULTILINE))
                self.assertRegex(requirements, re.compile(r"^pymysql==1\.1\.1", re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
