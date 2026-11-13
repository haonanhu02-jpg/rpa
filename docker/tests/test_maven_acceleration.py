"""Project Maven settings and mirror fallback without downloading dependencies."""

import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAVEN = ROOT / "docker/maven"


class MavenAccelerationTest(unittest.TestCase):
    def test_settings_cover_central_without_redirecting_private_repositories(self):
        ns = {"m": "http://maven.apache.org/SETTINGS/1.0.0"}
        for name in ("settings.xml", "settings-central.xml"):
            mirror = ET.parse(MAVEN / name).find("m:mirrors/m:mirror", ns)
            self.assertEqual(mirror.findtext("m:mirrorOf", namespaces=ns), "central")
        fallback = ET.parse(MAVEN / "settings-central.xml")
        self.assertEqual(
            fallback.findtext("m:mirrors/m:mirror/m:url", namespaces=ns), "https://repo.maven.apache.org/maven2"
        )

    def test_every_java_build_uses_project_wrapper_before_first_maven_command(self):
        for name in ("robot-service", "resource-service", "rpa-auth"):
            content = (ROOT / "backend" / name / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("COPY docker/maven/ /opt/astron-rpa-maven/", content)
            self.assertNotIn("RUN mvn ", content)
            self.assertIn("mvn-rpa.sh dependency:go-offline", content)
            self.assertIn("mvn-rpa.sh clean package", content)

    def run_wrapper(self, mode):
        if os.name == "nt":
            git = shutil.which("git")
            bash = Path(git).parents[1] / "bin/bash.exe" if git else Path("missing")
            if not bash.is_file():
                self.skipTest("Git Bash unavailable")

            def shell_path(path):
                return subprocess.check_output([str(bash), "-c", 'cygpath -u "$1"', "_", str(path)], text=True).strip()
        else:
            bash = shutil.which("bash")
            if not bash:
                self.skipTest("Bash unavailable")
            shell_path = str
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "mvn"
            mock.write_text(
                """#!/bin/sh
printf '<%s>' "$@" >> "$MAVEN_CALLS"
printf '\\n' >> "$MAVEN_CALLS"
[ "$MAVEN_TEST_MODE" != all_failure ] || exit 7
case "$*" in *settings-central.xml*) exit 0;; esac
[ "$MAVEN_TEST_MODE" = success ]
""",
                encoding="utf-8",
                newline="\n",
            )
            mock.chmod(0o755)
            result = subprocess.run(
                [
                    str(bash),
                    "-c",
                    """export PATH="$2:$PATH" MAVEN_CALLS="$2/calls" MAVEN_TEST_MODE="$3" MAVEN_SETTINGS_DIR="$2/settings"
sh "$1" clean package -DskipTests -P opensource
""",
                    "_",
                    shell_path(MAVEN / "mvn-rpa.sh"),
                    shell_path(root),
                    mode,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            return result, (root / "calls").read_text().splitlines()

    def test_mirror_success_never_retries_official(self):
        result, calls = self.run_wrapper("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("settings.xml", calls[0])
        self.assertIn("<clean><package><-DskipTests><-P><opensource>", calls[0])

    def test_mirror_failure_retries_same_command_with_official_settings(self):
        result, calls = self.run_wrapper("mirror_failure")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("settings-central.xml", calls[1])
        self.assertIn("<-U>", calls[1])
        self.assertTrue(all("<clean><package><-DskipTests><-P><opensource>" in call for call in calls))

    def test_both_sources_fail_preserves_failure_exit_code(self):
        result, calls = self.run_wrapper("all_failure")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
