#!/usr/bin/env python3
"""Prefetch only this project's images without changing Docker daemon settings."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BASE_SERVICES = ("mysql", "redis", "minio", "casdoor", "atlas", "openresty-nginx")
APPLICATIONS = ("ai-service", "openapi-service", "resource-service", "robot-service", "rpa-auth")
FROM = re.compile(r"^(FROM\s+(?:--platform=\S+\s+)?)(\S+)([^\r\n]*)$", re.MULTILINE | re.IGNORECASE)


def docker(*args, capture=True, timeout=35):
    return subprocess.run(
        ["docker", *args],
        cwd=ROOT,
        check=False,
        timeout=timeout,
        capture_output=capture,
        text=True,
        encoding="utf-8",
    )


def mirror_reference(reference, prefix):
    """Translate Docker Hub references only; other registries keep their origin."""
    parts = reference.split("/", 1)
    if len(parts) == 1:
        repository = "library/" + reference
    elif parts[0] in ("docker.io", "index.docker.io"):
        repository = parts[1]
        if "/" not in repository:
            repository = "library/" + repository
    elif "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        return reference
    else:
        repository = reference
    return prefix + "/" + repository


def mirror_prefixes():
    prefixes = []
    for value in os.environ.get("DOCKER_IMAGE_MIRRORS", "docker.m.daocloud.io").split(","):
        value = value.strip().rstrip("/")
        parsed = urlsplit(value if "://" in value else "https://" + value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DOCKER_IMAGE_MIRRORS 必须为逗号分隔的 HTTPS 镜像站域名/路径，不能带凭据或查询参数")
        prefixes.append(parsed.netloc + parsed.path)
    return list(dict.fromkeys(prefixes))


def cache_reference(reference):
    project = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:12]
    image = hashlib.sha256(reference.encode()).hexdigest()[:20]
    return f"astron-rpa-cache/{project}/{image}:cached"


def ensure_image(reference, prefixes, timeout):
    cached = cache_reference(reference)
    if os.environ.get("REFRESH_IMAGES", "0") != "1":
        for candidate in (cached, reference):
            if docker("image", "inspect", candidate).returncode == 0:
                if candidate != cached and docker("tag", candidate, cached).returncode:
                    raise ValueError("本项目镜像缓存标记失败：" + reference)
                print("复用本地镜像：" + reference, flush=True)
                return cached
    sources = list(dict.fromkeys([mirror_reference(reference, prefix) for prefix in prefixes] + [reference]))
    for source in sources:
        attempts = 1 if source == reference else 2
        for attempt in range(1, attempts + 1):
            print(f"拉取镜像 {reference}：{source}（{attempt}/{attempts}，最多 {timeout} 秒）", flush=True)
            try:
                result = docker("pull", source, capture=False, timeout=timeout)
            except subprocess.TimeoutExpired:
                print("镜像拉取超时，切换重试或下一来源。", flush=True)
                continue
            if result.returncode:
                print("该来源拉取失败，切换重试或下一来源。", flush=True)
                continue
            if docker("tag", source, cached).returncode:
                raise ValueError("本项目镜像缓存标记失败：" + reference)
            return cached
    raise ValueError(f"镜像 {reference} 经加速站和原站均拉取失败；请检查上方网络、镜像标签或架构错误")


def rewrite_dockerfile(content, resolve):
    stages = set()

    def replace(match):
        reference = match[2]
        suffix = match[3]
        result = reference
        if reference.lower() not in stages and reference.lower() != "scratch":
            if "$" in reference:
                raise ValueError("暂不支持含动态 FROM 镜像名的 Dockerfile")
            result = resolve(reference)
        alias = re.search(r"\s+AS\s+(\S+)", suffix, re.IGNORECASE)
        if alias:
            stages.add(alias[1].lower())
        return match[1] + result + suffix

    # Existing Dockerfiles need no external frontend; use Engine's bundled frontend.
    content = re.sub(r"^#\s*syntax=.*(?:\r?\n|$)", "", content, flags=re.MULTILINE)
    return FROM.sub(replace, content)


def atomic_write(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def prepare():
    timeout = int(os.environ.get("IMAGE_PULL_TIMEOUT", "600"))
    if timeout <= 0:
        raise ValueError("IMAGE_PULL_TIMEOUT 必须为正整数秒数")
    prefixes = mirror_prefixes()
    # Use Engine's local image store. This selects a builder for this process only.
    driver = docker("buildx", "inspect", "default", "--format", "{{.Driver}}")
    if driver.returncode or driver.stdout.strip() != "docker":
        raise ValueError("需要 Docker Buildx 的 default/docker 构建器，以复用本项目预拉取的镜像；未修改构建器配置")
    result = docker("compose", "config", "--format", "json")
    if result.returncode:
        raise ValueError("无法读取本项目 Compose 配置")
    config = json.loads(result.stdout)
    env_path = ROOT / ".env"
    if env_path.is_symlink():
        raise ValueError(".env 不能是符号链接")
    generated = ROOT / ".local/accelerated"
    if generated.is_symlink() or generated.parent.is_symlink():
        raise ValueError("镜像缓存配置目录不能是符号链接")
    generated.mkdir(parents=True, exist_ok=True)
    services = {}
    resolved = {}

    def resolve(reference):
        if reference not in resolved:
            resolved[reference] = ensure_image(reference, prefixes, timeout)
        return resolved[reference]

    for name in BASE_SERVICES:
        services[name] = {"image": resolve(config["services"][name]["image"]), "pull_policy": "never"}
    dockerfiles = {}
    for name in APPLICATIONS:
        source = ROOT.parent / "backend" / name / "Dockerfile"
        dockerfiles[name] = rewrite_dockerfile(source.read_text(encoding="utf-8"), resolve)
        services[name] = {"build": {"dockerfile": str(generated / f"Dockerfile.{name}"), "pull": False}}
    # Update project files only after every image has been fetched successfully.
    for name, content in dockerfiles.items():
        atomic_write(generated / f"Dockerfile.{name}", content)
    atomic_write(generated / "compose.json", json.dumps({"services": services}, indent=2) + "\n")
    spec = importlib.util.spec_from_file_location("credentials", ROOT / "scripts/sync-casdoor-credentials.py")
    credentials = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(credentials)
    content = credentials.update_env(
        env_path.read_text(encoding="utf-8"),
        {
            "COMPOSE_FILE": "docker-compose.yml:docker-compose.legacy-http.yml:docker-compose.local-build.yml:.local/accelerated/compose.json",
        },
    )
    atomic_write(env_path, content)
    print("项目镜像加速已就绪：仅写入本项目 .local 和 .env，未修改或重载 Docker 服务。", flush=True)


def main():
    try:
        prepare()
    except ValueError as error:
        print(str(error), flush=True)
        return 1
    except (OSError, subprocess.SubprocessError, KeyError, TypeError):
        print("项目镜像加速失败，请检查 Docker、Buildx、Compose 和文件权限", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
