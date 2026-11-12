"""Project-only image acceleration, with Docker operations mocked."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

DOCKER = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("project_images", DOCKER / "scripts/project-images.py")
IMAGES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGES)


class ProjectImagesTest(unittest.TestCase):
    def result(self, code=0, stdout=""):
        return subprocess.CompletedProcess([], code, stdout, "")

    def test_prepare_uses_supported_inspect_command_not_nonexistent_format_flag(self):
        def docker(*args, **kwargs):
            if args[0] == "buildx":
                if "--format" in args:
                    return subprocess.CompletedProcess(args, 1, "", "unknown flag: --format")
                if args[1] == "version":
                    return self.result(stdout="github.com/docker/buildx v0.12.1")
                return self.result(stdout="Name:          default\nDriver:        docker\nNodes:\n")
            if args[0] == "compose":
                return self.result(1)
            self.fail("Unexpected operation before configuration was checked")

        with (
            patch.object(IMAGES, "docker", side_effect=docker),
            self.assertRaisesRegex(ValueError, "无法读取本项目 Compose 配置"),
        ):
            IMAGES.prepare()

    def test_missing_buildx_is_reported_as_plugin_failure(self):
        with (
            patch.object(IMAGES, "docker", return_value=self.result(1)),
            self.assertRaisesRegex(ValueError, "Buildx 插件不可用"),
        ):
            IMAGES.prepare()

    def test_non_docker_driver_reports_actual_driver_without_modifying_builder(self):
        calls = []

        def docker(*args, **kwargs):
            calls.append(args)
            if args == ("buildx", "version"):
                return self.result(stdout="github.com/docker/buildx v0.12.1")
            return self.result(stdout="Name: default\nDriver: docker-container\n")

        with patch.object(IMAGES, "docker", side_effect=docker), self.assertRaisesRegex(ValueError, "docker-container"):
            IMAGES.prepare()
        self.assertEqual(calls, [("buildx", "version"), ("buildx", "inspect", "default")])

    def test_inspect_failure_is_not_misreported_as_missing_plugin(self):
        def docker(*args, **kwargs):
            return self.result(0 if args == ("buildx", "version") else 1)

        with (
            patch.object(IMAGES, "docker", side_effect=docker),
            self.assertRaisesRegex(ValueError, "无法读取 default 构建器"),
        ):
            IMAGES.prepare()

    def test_mirror_reference_keeps_repository_tag_and_digest(self):
        for source, expected in (
            ("mysql:8.4.6", "library/mysql:8.4.6"),
            ("docker.io/python:3.13-slim", "library/python:3.13-slim"),
            ("bitnami/redis:latest", "bitnami/redis:latest"),
            ("minio/minio@sha256:abc", "minio/minio@sha256:abc"),
        ):
            self.assertEqual(IMAGES.mirror_reference(source, "mirror.example"), "mirror.example/" + expected)
        self.assertEqual(IMAGES.mirror_reference("ghcr.io/org/image:v1", "mirror.example"), "ghcr.io/org/image:v1")

    def test_custom_mirror_requires_https_and_no_embedded_credentials(self):
        for value in (
            "http://mirror.example",
            "https://user:secret@mirror.example",
            "https://mirror.example?token=secret",
        ):
            with patch.dict(os.environ, {"DOCKER_IMAGE_MIRRORS": value}), self.assertRaises(ValueError):
                IMAGES.mirror_prefixes()

    def test_mirror_failure_retries_then_falls_back_and_uses_project_tag(self):
        calls = []

        def docker(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("image", "inspect"):
                return self.result(1)
            if args[0] == "pull" and args[1].startswith("mirror.example/"):
                return self.result(1)
            return self.result()

        with patch.object(IMAGES, "docker", side_effect=docker):
            cached = IMAGES.ensure_image("mysql:8.4.6", ["mirror.example"], 10)
        pulls = [call for call in calls if call[0] == "pull"]
        self.assertEqual(pulls, [("pull", "mirror.example/library/mysql:8.4.6")] * 2 + [("pull", "mysql:8.4.6")])
        self.assertTrue(cached.startswith("astron-rpa-cache/"))
        self.assertIn(("tag", "mysql:8.4.6", cached), calls)
        self.assertFalse(any(call[0] == "tag" and call[-1] == "mysql:8.4.6" for call in calls))

    def test_failed_sources_report_specific_image(self):
        with (
            patch.object(IMAGES, "docker", return_value=self.result(1)),
            self.assertRaisesRegex(ValueError, "bitnami/redis:latest"),
        ):
            IMAGES.ensure_image("bitnami/redis:latest", ["mirror.example"], 10)

    def test_cached_image_avoids_download(self):
        with patch.object(IMAGES, "docker", return_value=self.result()) as docker:
            IMAGES.ensure_image("mysql:8.4.6", ["mirror.example"], 10)
        self.assertEqual(docker.call_count, 1)

    def test_pull_timeout_falls_back(self):
        def docker(*args, **kwargs):
            if args[:2] == ("image", "inspect"):
                return self.result(1)
            if args[0] == "pull" and args[1].startswith("mirror.example/"):
                raise subprocess.TimeoutExpired("docker", 1)
            return self.result()

        with patch.object(IMAGES, "docker", side_effect=docker):
            self.assertTrue(IMAGES.ensure_image("mysql:8.4.6", ["mirror.example"], 1).startswith("astron-rpa-cache/"))

    def test_rewrite_preserves_build_platform_stages_and_copy(self):
        source = "# syntax=docker/dockerfile:1\nFROM --platform=$BUILDPLATFORM maven:21 AS build\nRUN mvn package\nFROM build AS test\nFROM eclipse-temurin:21 AS runtime\nCOPY --from=build /app/a.jar /app/a.jar\n"
        references = []

        def resolve(reference):
            references.append(reference)
            return "project/" + reference

        output = IMAGES.rewrite_dockerfile(source, resolve)
        self.assertEqual(references, ["maven:21", "eclipse-temurin:21"])
        self.assertIn("FROM --platform=$BUILDPLATFORM project/maven:21 AS build", output)
        self.assertIn("FROM build AS test", output)
        self.assertIn("COPY --from=build", output)
        self.assertNotIn("# syntax=", output)

    def test_generated_compose_is_project_scoped_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker_root = root / "docker"
            (docker_root / "scripts").mkdir(parents=True)
            (docker_root / "scripts/sync-casdoor-credentials.py").write_bytes(
                (DOCKER / "scripts/sync-casdoor-credentials.py").read_bytes()
            )
            env = "DATABASE_PASSWORD='private-db'\nMINIO_SK='private-minio'\nCOMPOSE_FILE='docker-compose.yml'\n"
            (docker_root / ".env").write_text(env)
            for name in IMAGES.APPLICATIONS:
                path = root / "backend" / name
                path.mkdir(parents=True)
                (path / "Dockerfile").write_text("FROM python:3.13-slim\nCOPY backend/app /app\n")
            config = {"services": {name: {"image": f"{name}:test"} for name in IMAGES.BASE_SERVICES}}

            def docker(*args, **kwargs):
                if args[0] == "buildx":
                    return self.result(stdout="Name: default\nDriver: docker\nNodes:\n")
                if args[0] == "compose":
                    return self.result(stdout=json.dumps(config))
                self.fail("Unexpected Docker operation: " + str(args))

            with (
                patch.object(IMAGES, "ROOT", docker_root),
                patch.object(IMAGES, "docker", side_effect=docker),
                patch.object(IMAGES, "ensure_image", side_effect=lambda reference, *_: "project/" + reference),
            ):
                IMAGES.prepare()
                first = (docker_root / ".env").read_text()
                IMAGES.prepare()
                self.assertEqual((docker_root / ".env").read_text(), first)
            self.assertIn("DATABASE_PASSWORD='private-db'", first)
            self.assertIn("MINIO_SK='private-minio'", first)
            self.assertIn(".local/accelerated/compose.json", first)
            generated = json.loads((docker_root / ".local/accelerated/compose.json").read_text())
            self.assertEqual(generated["services"]["mysql"]["pull_policy"], "never")
            for name in IMAGES.APPLICATIONS:
                self.assertFalse(generated["services"][name]["build"]["pull"])
                self.assertIn(
                    "FROM project/python:3.13-slim",
                    Path(generated["services"][name]["build"]["dockerfile"]).read_text(),
                )

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI unavailable")
    def test_actual_compose_accepts_generated_overrides_and_original_dockerfiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker_root = root / "docker"
            (docker_root / "scripts").mkdir(parents=True)
            for name in ("docker-compose.yml", "docker-compose.legacy-http.yml", "docker-compose.local-build.yml"):
                shutil.copy(DOCKER / name, docker_root / name)
            shutil.copy(DOCKER / "scripts/sync-casdoor-credentials.py", docker_root / "scripts")
            (docker_root / ".env").write_text(
                "COMPOSE_PATH_SEPARATOR=:\n"
                "COMPOSE_FILE=docker-compose.yml:docker-compose.legacy-http.yml:docker-compose.local-build.yml\n"
                + (DOCKER / ".env.example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            for name in IMAGES.APPLICATIONS:
                path = root / "backend" / name
                path.mkdir(parents=True)
                shutil.copy(DOCKER.parent / "backend" / name / "Dockerfile", path / "Dockerfile")

            def compose_config():
                result = subprocess.run(
                    ["docker", "compose", "config", "--format", "json"],
                    cwd=docker_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, "Compose rejected project configuration")
                return json.loads(result.stdout)

            original = compose_config()

            def docker(*args, **kwargs):
                return self.result(
                    stdout="Name: default\nDriver: docker\nNodes:\n" if args[0] == "buildx" else json.dumps(original)
                )

            with (
                patch.object(IMAGES, "ROOT", docker_root),
                patch.object(IMAGES, "docker", side_effect=docker),
                patch.object(
                    IMAGES, "ensure_image", side_effect=lambda reference, *_: IMAGES.cache_reference(reference)
                ),
            ):
                IMAGES.prepare()
            actual = compose_config()
            for name in IMAGES.BASE_SERVICES:
                self.assertTrue(actual["services"][name]["image"].startswith("astron-rpa-cache/"))
                self.assertEqual(actual["services"][name]["pull_policy"], "never")
            for name in IMAGES.APPLICATIONS:
                build = actual["services"][name]["build"]
                self.assertFalse(build.get("pull", False))
                content = Path(build["dockerfile"]).read_text(encoding="utf-8")
                self.assertNotIn("# syntax=", content)
                self.assertTrue(
                    "FROM astron-rpa-cache/" in content or "FROM --platform=$BUILDPLATFORM astron-rpa-cache/" in content
                )


if __name__ == "__main__":
    unittest.main()
