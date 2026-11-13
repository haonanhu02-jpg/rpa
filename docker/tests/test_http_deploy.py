"""Deployment regressions without requiring a running Docker engine."""

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
SPEC = importlib.util.spec_from_file_location("http_deploy", DOCKER / "scripts/http-deploy.py")
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


class HttpDeployTest(unittest.TestCase):
    def test_databases_create_missing_catalogs_idempotently_without_password_arguments(
        self,
    ):
        config = {
            "name": "astron-rpa",
            "services": {
                "rpa-auth": {
                    "environment": {
                        "DATABASE_NAME": "rpa",
                        "CASDOOR_DATABASE_NAME": "casdoor",
                    }
                }
            },
        }
        with patch.object(
            DEPLOY,
            "compose",
            side_effect=[json.dumps(config), "", "1", "1", "1", "1", "1"],
        ) as compose:
            DEPLOY.databases("192.168.3.110")
        command = compose.call_args_list[1]
        self.assertIn("CREATE DATABASE IF NOT EXISTS `casdoor`", command.kwargs["data"])
        self.assertNotIn("DROP", command.kwargs["data"])
        self.assertIn('MYSQL_PWD="$MYSQL_ROOT_PASSWORD"', command.args[-1])
        self.assertFalse(any("TRUNCATE" in call.kwargs.get("data", "") for call in compose.call_args_list))

    def test_databases_repairs_empty_initialized_volume(self):
        config = {
            "name": "astron-rpa",
            "services": {
                "rpa-auth": {
                    "environment": {
                        "DATABASE_NAME": "rpa",
                        "CASDOOR_DATABASE_NAME": "casdoor",
                    }
                }
            },
        }
        directory = self.root / "volumes/mysql"
        directory.mkdir(parents=True)
        filenames = [
            "schema.sql",
            "init_app_market_dict_data.sql",
            "init_his_data_enum_data.sql",
            "init_sample_template_data.sql",
            "init_c_atom_meta_new_data.sql",
        ]
        for filename in filenames:
            (directory / filename).write_text("-- fixture " + filename, encoding="utf-8")
        responses = [json.dumps(config), "", "0", ""] + ["0", ""] * 4
        with patch.object(DEPLOY, "compose", side_effect=responses) as compose:
            DEPLOY.databases("192.168.3.110")
        queries = [call.kwargs.get("data", "") for call in compose.call_args_list]
        for filename in filenames:
            self.assertTrue(any("-- fixture " + filename in query for query in queries))

    def test_materials_fixes_bind_mount_assets_without_changing_secrets(self):
        for directory in ("mysql", "nginx", "atlas", "casdoor"):
            path = self.root / "volumes" / directory
            path.mkdir(parents=True)
            path.chmod(0o700)
        config = self.root / "volumes/mysql/my.cnf"
        config.write_text("[mysqld]\n")
        config.chmod(0o600)
        secret = self.root / ".env"
        secret.write_text("private")
        secret.chmod(0o600)
        DEPLOY.materials("192.168.3.110")
        self.assertEqual(secret.read_text(), "private")
        if os.name != "nt":
            self.assertEqual(config.stat().st_mode & 0o777, 0o644)
            self.assertEqual(config.parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)

    def test_databases_rejects_foreign_project(self):
        with (
            patch.object(DEPLOY, "compose", return_value='{"name":"docker"}'),
            self.assertRaisesRegex(ValueError, "拒绝"),
        ):
            DEPLOY.databases("192.168.3.110")

    def test_guard_rejects_shared_docker_project_before_container_operations(self):
        with (
            patch.object(DEPLOY, "compose", return_value='{"name":"docker","services":{}}'),
            patch.object(DEPLOY.subprocess, "run") as run,
            self.assertRaisesRegex(ValueError, "项目名"),
        ):
            DEPLOY.guard("192.168.3.110")
        run.assert_not_called()

    def test_guard_refuses_to_adopt_foreign_project_container(self):
        config = '{"name":"astron-rpa","services":{"redis":{"container_name":"rpa-opensource-redis"}}}'
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            output = "old-id" if args[1] == "ps" else '[{"Config":{"Labels":{"com.docker.compose.project":"docker"}}}]'
            return subprocess.CompletedProcess(args, 0, output, "")

        with (
            patch.object(DEPLOY, "compose", return_value=config),
            patch.object(DEPLOY.subprocess, "run", side_effect=run),
            self.assertRaisesRegex(ValueError, "归属其他项目"),
        ):
            DEPLOY.guard("192.168.3.110")
        self.assertEqual([call[1] for call in calls], ["ps", "inspect"])

    def test_guard_allows_empty_isolated_project(self):
        config = '{"name":"astron-rpa","services":{"redis":{"container_name":"rpa-opensource-redis"}}}'
        with (
            patch.object(DEPLOY, "compose", return_value=config),
            patch.object(
                DEPLOY.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            DEPLOY.guard("192.168.3.110")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        shutil.copy(DOCKER / ".env.example", self.root / ".env.example")
        shutil.copy(DOCKER / "scripts/sync-casdoor-credentials.py", self.root / "scripts")
        self.root_patch = patch.object(DEPLOY, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def test_first_install_generates_consistent_credentials(self):
        DEPLOY.prepare("192.168.3.110")
        content = (self.root / ".env").read_text()

        def value(key):
            return next(
                line.split("=", 1)[1].strip("'\"") for line in content.splitlines() if line.startswith(key + "=")
            )

        password = value("DATABASE_PASSWORD")
        self.assertEqual(len(password), 48)
        self.assertEqual(value("CASDOOR_DATABASE_PASSWORD"), password)
        self.assertIn(":" + password + "@", value("ATLAS_URL"))
        self.assertIn(":" + password + "@", value("ATLAS_HOST_URL"))
        self.assertNotEqual(value("MINIO_SK"), password)
        self.assertNotEqual(value("MINIO_AK"), "rpaminioadmin")
        self.assertEqual(value("LEGACY_CASDOOR_EXTERNAL_ENDPOINT"), "http://192.168.3.110:8000")
        self.assertEqual(value("SESSION_COOKIE_SECURE"), "false")
        self.assertGreaterEqual(len(value("MONITORING_TOKEN")), 32)
        if os.name != "nt":
            self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)

    def test_repeat_preserves_passwords_and_multiline_certificate(self):
        DEPLOY.prepare("192.168.3.110")
        path = self.root / ".env"
        content = path.read_text().replace('CASDOOR_CERTIFICATE=""', "CASDOOR_CERTIFICATE='line1\nline2'")
        path.write_text(content)
        DEPLOY.prepare("192.168.3.110")
        self.assertEqual(path.read_text(), content)

    def test_invalid_existing_configuration_is_not_replaced(self):
        path = self.root / ".env"
        path.write_text("DATABASE_PASSWORD=\nMINIO_AK=\n")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            DEPLOY.prepare("192.168.3.110")
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_address_does_not_create_configuration(self):
        with self.assertRaises(ValueError):
            DEPLOY.prepare("192.168.3.110; echo bad")
        self.assertFalse((self.root / ".env").exists())

    def mock_compose(self, *args):
        if args[0] == "config":
            return json.dumps({"services": {"openresty-nginx": {"environment": {"DEPLOYMENT_MODE": "legacy-http"}}}})
        return "container-id\n"

    def mock_run(self, args, **kwargs):
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps([{"State": {"Status": "running", "Health": {"Status": "healthy"}}}]),
                "",
            )
        return subprocess.CompletedProcess(args, 0, "内部服务检查通过", "")

    @staticmethod
    def mock_request(url):
        if url.endswith("/health"):
            return 200, "healthy\n"
        if url.endswith("login-check"):
            return 200, '{"code":"900001"}'
        return 200, "<!doctype html><html>Casdoor</html>"

    def test_success_requires_internal_checks_and_public_endpoints(self):
        with (
            patch.object(DEPLOY, "compose", side_effect=self.mock_compose),
            patch.object(DEPLOY.subprocess, "run", side_effect=self.mock_run),
            patch.object(DEPLOY, "request", side_effect=self.mock_request) as request,
        ):
            DEPLOY.verify("192.168.3.110")
        self.assertEqual(request.call_count, 6)

    def test_gateway_healthy_does_not_hide_broken_auth(self):
        def response(url):
            return (502, "bad gateway") if url.endswith("login-check") else self.mock_request(url)

        with (
            patch.object(DEPLOY, "compose", side_effect=self.mock_compose),
            patch.object(DEPLOY.subprocess, "run", side_effect=self.mock_run),
            patch.object(DEPLOY, "request", side_effect=response),
            self.assertRaisesRegex(ValueError, "认证 API"),
        ):
            DEPLOY.verify("192.168.3.110")

    def test_internal_failure_reports_service_without_leaking_stderr(self):
        def run(args, **kwargs):
            if args[:2] == ["docker", "inspect"]:
                return self.mock_run(args, **kwargs)
            return subprocess.CompletedProcess(args, 1, "openapi-service 内部 HTTP/依赖就绪检查失败", "secret")

        with (
            patch.object(DEPLOY, "compose", side_effect=self.mock_compose),
            patch.object(DEPLOY.subprocess, "run", side_effect=run),
            self.assertRaisesRegex(ValueError, "openapi-service") as error,
        ):
            DEPLOY.verify("192.168.3.110")
        self.assertNotIn("secret", str(error.exception))

    def test_exited_container_is_a_failure(self):
        result = subprocess.CompletedProcess([], 0, json.dumps([{"State": {"Status": "exited"}}]), "")
        with (
            patch.object(DEPLOY, "compose", side_effect=self.mock_compose),
            patch.object(DEPLOY.subprocess, "run", return_value=result),
            self.assertRaisesRegex(ValueError, "mysql"),
        ):
            DEPLOY.verify("192.168.3.110")


class ShellDeploymentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name == "nt":
            git = shutil.which("git")
            candidate = Path(git).parents[1] / "bin/bash.exe" if git else Path("missing")
            cls.bash = str(candidate) if candidate.is_file() else None
        else:
            cls.bash = shutil.which("bash")
        if not cls.bash:
            raise unittest.SkipTest("Bash unavailable")

    def shell_path(self, path):
        if os.name != "nt":
            return str(path)
        return subprocess.check_output([self.bash, "-c", 'cygpath -u "$1"', "_", str(path)], text=True).strip()

    def run_deployment(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            (app / ".git").mkdir(parents=True)
            (app / "docker/scripts").mkdir(parents=True)
            (app / "docker/scripts/http-deploy.py").touch()
            if scenario != "old_database":
                (app / "docker/.env").touch()
            tools = root / "bin"
            tools.mkdir()
            mocks = {
                "git": """echo "git $*" >> "$MOCK_CALLS"
case "$*" in
*"branch --show-current"*) echo main;;
*fetch*)
 echo "$*" > "$MOCK_CALLS.source"
 echo 'mock-git-download-progress'
 if [ "$SCENARIO" = fetch_timeout ]; then /usr/bin/sleep 3; fi
 [ "$SCENARIO" != git_failure ] || exit 1
 if [ "$SCENARIO" = mirror_failure ]; then
  case "$*" in *ghfast.top*) exit 1;; esac
 fi;;
*cat-file*)
 if [ "$SCENARIO" = stale_mirror ] && grep -q ghfast.top "$MOCK_CALLS.source"; then exit 1; fi;;
*) exit 0;;
esac
""",
                "docker": """echo "$*" >> "$MOCK_CALLS"
case "$*" in
"compose version --short") echo 2.24.4;;
"info --format"*) echo linux;;
"ps -aq"*) [ "$SCENARIO" != old_database ] || echo old-mysql;;
"compose build") [ "$SCENARIO" != build_failure ];;
*) exit 0;;
esac
""",
                "python3": """case "$*" in
*" guard "*)
 if [ "$SCENARIO" = project_collision ]; then
  echo '容器归属其他项目，已停止部署'
  exit 1
 fi;;
*" databases "*)
 if [ "$SCENARIO" = database_failure ]; then exit 1; fi;;
*" verify "*)
 if [ "$SCENARIO" = readiness_failure ]; then
  echo 'openapi-service 内部 HTTP/依赖就绪检查失败'
  exit 1
 fi;;
*sync-casdoor-credentials.py*) [ "$SCENARIO" != sync_failure ];;
*project-images.py*)
 if [ "$SCENARIO" = image_failure ]; then
  echo 'mysql:8.4.6 加速站与原站均拉取失败'
  exit 1
 fi;;
*) exit 0;;
esac
""",
                "flock": "exit 0\n",
                "sleep": "exit 0\n",
            }
            for name, content in mocks.items():
                path = tools / name
                path.write_text("#!/usr/bin/env bash\n" + content, encoding="utf-8", newline="\n")
                path.chmod(0o755)
            command = """export APP_DIR="$2/app" TMPDIR="$2" MOCK_CALLS="$2/calls" PATH="$2/bin:$PATH" DEPLOY_TIMEOUT=1 GIT_FETCH_TIMEOUT=1
export SCENARIO="$3"
bash "$1"
"""
            result = subprocess.run(
                [
                    self.bash,
                    "-c",
                    command,
                    "_",
                    self.shell_path(DOCKER.parent / "deploy-http.sh"),
                    self.shell_path(root),
                    scenario,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
            )
            calls = (root / "calls").read_text() if (root / "calls").exists() else ""
            return result, calls

    def test_success_prints_verified_links_and_never_deletes_volumes(self):
        result, calls = self.run_deployment("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("部署成功", result.stdout)
        self.assertIn("http://192.168.3.110:8000/", result.stdout)
        self.assertIn("http://192.168.3.110:32742/health", result.stdout)
        self.assertNotIn(" down", calls)
        self.assertNotIn("volume rm", calls)

    def test_build_failure_does_not_restart_services(self):
        result, calls = self.run_deployment("build_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("构建当前后端源码", result.stderr)
        self.assertNotIn("compose up", calls)
        self.assertNotIn("部署成功", result.stdout)

    def test_catalog_creation_failure_does_not_start_casdoor(self):
        result, calls = self.run_deployment("database_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("确认业务和认证数据库", result.stderr)
        self.assertFalse(any(line.startswith("compose up") and line.endswith("casdoor") for line in calls.splitlines()))

    def test_project_collision_stops_before_build_or_container_start(self):
        result, calls = self.run_deployment("project_collision")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("检查部署项目隔离", result.stderr)
        self.assertNotIn("compose build", calls)
        self.assertNotIn("compose up", calls)

    def test_image_acceleration_failure_never_builds_or_starts_services(self):
        result, calls = self.run_deployment("image_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("本项目 Docker 镜像", result.stderr)
        self.assertIn("mysql:8.4.6", result.stdout)
        self.assertNotIn("compose build", calls)
        self.assertNotIn("compose up", calls)
        self.assertNotIn("daemon.json", calls)
        self.assertNotIn("restart", calls)

    def test_failed_readiness_prints_service_reason_without_success(self):
        result, _ = self.run_deployment("readiness_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("openapi-service", result.stderr)
        self.assertIn("部署失败", result.stderr)
        self.assertNotIn("部署成功", result.stdout)

    def test_failed_fetch_never_builds_or_restarts(self):
        result, calls = self.run_deployment("git_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("获取项目代码", result.stderr)
        self.assertNotIn("compose build", calls)
        self.assertNotIn("compose up", calls)
        fetches = [line for line in calls.splitlines() if " fetch " in line]
        self.assertEqual(len(fetches), 4)
        self.assertTrue(all("ghfast.top" in line for line in fetches[:3]))
        self.assertNotIn("ghfast.top", fetches[3])

    def test_mirror_success_shows_progress_without_using_origin(self):
        result, calls = self.run_deployment("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fetches = [line for line in calls.splitlines() if " fetch " in line]
        self.assertEqual(len(fetches), 1)
        self.assertIn("https://ghfast.top/https://github.com/haonanhu02-jpg/rpa.git", fetches[0])
        self.assertIn("--progress", fetches[0])
        self.assertIn("mock-git-download-progress", result.stdout)

    def test_three_failed_mirror_attempts_fall_back_to_origin(self):
        result, calls = self.run_deployment("mirror_failure")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fetches = [line for line in calls.splitlines() if " fetch " in line]
        self.assertEqual(len(fetches), 4)
        self.assertTrue(all("ghfast.top" in line for line in fetches[:3]))
        self.assertNotIn("ghfast.top", fetches[3])
        self.assertIn("回退 GitHub 原站", result.stdout)

    def test_stale_code_mirror_falls_back_before_running_old_helpers(self):
        result, calls = self.run_deployment("stale_mirror")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fetches = [line for line in calls.splitlines() if " fetch " in line]
        self.assertEqual(len(fetches), 2)
        self.assertIn("ghfast.top", fetches[0])
        self.assertNotIn("ghfast.top", fetches[1])
        self.assertIn("尚未同步", result.stdout)

    def test_stalled_fetch_times_out_without_restarting_services(self):
        result, calls = self.run_deployment("fetch_timeout")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败或超时", result.stdout)
        self.assertIn("镜像重试 3 次及 GitHub 原站回退均失败", result.stderr)
        self.assertNotIn("compose up", calls)

    def test_missing_env_with_existing_database_stops_before_build(self):
        result, calls = self.run_deployment("old_database")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("恢复原配置", result.stderr)
        self.assertNotIn("compose build", calls)
        self.assertNotIn("compose up", calls)

    def test_auth_sync_failure_stops_before_migration_and_application_start(self):
        result, calls = self.run_deployment("sync_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("同步 Casdoor 凭据", result.stderr)
        self.assertNotIn("run --rm atlas", calls)
        self.assertNotIn("--no-deps ai-service", calls)


if __name__ == "__main__":
    unittest.main()
