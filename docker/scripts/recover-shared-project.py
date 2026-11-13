#!/usr/bin/env python3
"""Recover the failed legacy RPA install, preserving volumes and Dify configuration."""

import argparse
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("rpa-opensource-casdoor", "rpa-opensource-mysql", "rpa-opensource-redis", "rpa-opensource-minio")
DIFY = Path("/opt/app-stack/dify/docker")


def run(args, **kwargs):
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=90, **kwargs)
    if result.returncode:
        raise ValueError("命令失败：" + " ".join(args[:4]) + "；未删除任何数据卷，请检查该服务日志")
    return result.stdout


def inspect(name, kind=None):
    return json.loads(run(["docker", *([kind] if kind else []), "inspect", name]))[0]


def require_empty_mysql_volume(volume):
    mount = Path(volume["Mountpoint"])
    if not mount.is_absolute() or not mount.is_dir():
        raise ValueError("无法在本机只读检查 MySQL 卷目录；必须在 Docker 服务器以 root 执行")
    if any(path.name != "lost+found" for path in mount.iterdir()):
        raise ValueError("MySQL 数据卷非空，禁止将已有数据库切换到 8.0；未移除容器或数据")


def load_helper(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recover(apply=False):
    env_file = ROOT / ".env"
    if env_file.is_symlink() or not env_file.is_file():
        raise ValueError("必须保留原 .env，不能重新生成数据库密码")
    containers = [inspect(name) for name in NAMES]
    for name, container in zip(NAMES, containers):
        labels = container.get("Config", {}).get("Labels", {}) or {}
        if container.get("Name", "").lstrip("/") != name or labels.get("com.docker.compose.project") != "docker":
            raise ValueError("容器归属与本次旧部署不符，禁止自动移除：" + name)
        if Path(labels.get("com.docker.compose.project.working_dir", "")).resolve() != ROOT.resolve():
            raise ValueError("容器不是来自当前 RPA 目录，已停止：" + name)
    mysql = containers[1]
    volumes = [
        m["Name"] for m in mysql["Mounts"] if m.get("Destination") == "/var/lib/mysql" and m.get("Type") == "volume"
    ]
    if len(volumes) != 1:
        raise ValueError("无法唯一确定原 MySQL 数据卷")
    volume_name = volumes[0]
    require_empty_mysql_volume(inspect(volume_name, "volume"))
    labels = inspect("docker-api-1")["Config"]["Labels"]
    if (
        labels.get("com.docker.compose.project") != "docker"
        or Path(labels.get("com.docker.compose.project.working_dir", "")).resolve() != DIFY
    ):
        raise ValueError("Dify 来源不匹配，未操作其他项目")
    files = [Path(p) for p in labels.get("com.docker.compose.project.config_files", "").split(",") if p]
    if not files or any(not p.is_file() or DIFY not in p.resolve().parents for p in files):
        raise ValueError("无法确认 Dify 原 Compose 文件，未操作其他项目")
    environment = dict(os.environ)
    for key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PATH_SEPARATOR"):
        environment.pop(key, None)
    dify_command = ["docker", "compose", "--project-directory", str(DIFY), "-p", "docker"]
    for path in files:
        dify_command += ["-f", str(path)]
    dify_config = json.loads(run(dify_command + ["config", "--format", "json"], cwd=DIFY, env=environment))
    if "redis" not in dify_config["services"]:
        raise ValueError("Dify 原配置中缺少 Redis，已停止")
    print("已确认：原 RPA MySQL 卷为空；将保留卷 " + volume_name)
    print("仅移除四个失败的 RPA 容器；用 Dify 原配置恢复 redis；然后使用独立 astron-rpa 项目。")
    if not apply:
        print("当前为只读检查；确认后可使用 --apply 执行。")
        return
    images = load_helper("project_images", "project-images.py")
    for reference, executable in (("mysql:8.0.43-debian", "mysqld"), ("redis:7.4.7-alpine", "redis-server")):
        cached = images.ensure_image(reference, images.mirror_prefixes(), 600)
        run(["docker", "run", "--rm", "--entrypoint", executable, cached, "--version"])
    # Preserve the original credentials before modifying only deployment settings.
    backup = ROOT / ".local/recovery"
    backup.mkdir(parents=True, exist_ok=True)
    old_content = env_file.read_text(encoding="utf-8")
    images.atomic_write(backup / ("env-before-isolation-" + str(time.time_ns())), old_content)
    credentials = load_helper("credentials", "sync-casdoor-credentials.py")
    content = credentials.update_env(
        old_content,
        {
            "COMPOSE_PROJECT_NAME": "astron-rpa",
            "MYSQL_IMAGE": "mysql:8.0.43-debian",
            "RPA_MYSQL_VOLUME": volume_name,
            "RPA_MYSQL_VOLUME_EXTERNAL": "true",
        },
    )
    images.atomic_write(env_file, content)
    # Exact names and ownership were verified above. Never remove volumes or orphans.
    run(["docker", "rm", "--force", *NAMES])
    run(dify_command + ["up", "-d", "--no-deps", "redis"], cwd=DIFY, env=environment)
    print("隔离恢复操作完成：Dify Redis 已按原配置启动，数据卷未删除。")
    print("下一步：bash " + str(ROOT.parent / "deploy-http.sh"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        import fcntl

        with Path(str(ROOT.parent) + ".deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            recover(args.apply)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print("恢复未完成：" + str(error))
        raise SystemExit(1) from None
