"""Safety boundaries for recovery from a shared Compose project."""

import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

DOCKER = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("recovery", DOCKER / "scripts/recover-shared-project.py")
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)


class RecoveryTest(unittest.TestCase):
    def test_apply_only_removes_verified_rpa_containers_and_restores_dify_redis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "rpa/docker"
            root.mkdir(parents=True)
            (root / ".env").write_text("DATABASE_PASSWORD=keep\n")
            data = Path(directory) / "mysql-data"
            data.mkdir()
            dify = Path(directory) / "dify/docker"
            dify.mkdir(parents=True)
            compose = dify / "compose.yaml"
            compose.touch()
            calls = []

            def inspect(name, kind=None):
                if kind == "volume":
                    return {"Mountpoint": str(data)}
                if name == "docker-api-1":
                    return {
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": "docker",
                                "com.docker.compose.project.working_dir": str(dify),
                                "com.docker.compose.project.config_files": str(compose),
                            }
                        }
                    }
                return {
                    "Name": "/" + name,
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "docker",
                            "com.docker.compose.project.working_dir": str(root),
                        }
                    },
                    "Mounts": [{"Type": "volume", "Name": "docker_mysql_data", "Destination": "/var/lib/mysql"}],
                }

            def run(args, **kwargs):
                calls.append(args)
                return json.dumps({"services": {"redis": {}}}) if "config" in args else ""

            images = types.SimpleNamespace(
                ensure_image=lambda *args: "rpa-cache/image",
                mirror_prefixes=list,
                atomic_write=lambda path, content: path.write_text(content),
            )
            spec = importlib.util.spec_from_file_location("credentials", DOCKER / "scripts/sync-casdoor-credentials.py")
            credentials = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(credentials)
            with (
                patch.object(RECOVERY, "ROOT", root),
                patch.object(RECOVERY, "DIFY", dify),
                patch.object(RECOVERY, "inspect", side_effect=inspect),
                patch.object(RECOVERY, "run", side_effect=run),
                patch.object(
                    RECOVERY,
                    "load_helper",
                    side_effect=lambda name, _: images if name == "project_images" else credentials,
                ),
            ):
                RECOVERY.recover(apply=True)
            removals = [args for args in calls if args[1] == "rm"]
            self.assertEqual(removals, [["docker", "rm", "--force", *RECOVERY.NAMES]])
            self.assertEqual(calls[-1][-4:], ["up", "-d", "--no-deps", "redis"])
            self.assertTrue(data.is_dir())
            content = (root / ".env").read_text()
            self.assertIn("DATABASE_PASSWORD=keep", content)
            self.assertIn("RPA_MYSQL_VOLUME='docker_mysql_data'", content)

    def test_initialized_mysql_volume_is_never_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "auto.cnf").write_text("existing mysql data")
            with self.assertRaisesRegex(ValueError, "禁止"):
                RECOVERY.require_empty_mysql_volume({"Mountpoint": directory})
            self.assertTrue((Path(directory) / "auto.cnf").exists())

    def test_empty_volume_is_eligible_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            RECOVERY.require_empty_mysql_volume({"Mountpoint": directory})
            self.assertTrue(Path(directory).is_dir())

    def test_foreign_container_stops_recovery_before_any_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("DATABASE_PASSWORD=keep")
            data = {"Name": "/rpa-opensource-mysql", "Config": {"Labels": {"com.docker.compose.project": "other"}}}
            with (
                patch.object(RECOVERY, "ROOT", root),
                patch.object(RECOVERY, "inspect", return_value=data),
                patch.object(RECOVERY, "run") as run,
                self.assertRaises(ValueError),
            ):
                RECOVERY.recover(apply=True)
            run.assert_not_called()
            self.assertEqual((root / ".env").read_text(), "DATABASE_PASSWORD=keep")


if __name__ == "__main__":
    unittest.main()
