#!/usr/bin/env python3
"""Private configuration and read-only readiness checks for deploy-http.sh."""

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    "mysql",
    "redis",
    "minio",
    "casdoor",
    "ai-service",
    "openapi-service",
    "resource-service",
    "robot-service",
    "rpa-auth",
    "openresty-nginx",
)


def compose(*args, data=None):
    result = subprocess.run(
        ["docker", "compose", *args],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input=data,
        timeout=35,
    )
    if result.returncode:
        raise ValueError("Docker Compose 操作失败，请查看容器状态和私有部署日志")
    return result.stdout


def prepare(server_ip):
    ipaddress.IPv4Address(server_ip)
    path = ROOT / ".env"
    if path.is_symlink():
        raise ValueError(".env 不能是符号链接")
    existing = path.exists()
    content = (path if existing else ROOT / ".env.example").read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("credentials", ROOT / "scripts/sync-casdoor-credentials.py")
    credentials = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(credentials)
    values = {
        "COMPOSE_PROJECT_NAME": "astron-rpa",
        "COMPOSE_PATH_SEPARATOR": ":",
        "COMPOSE_FILE": "docker-compose.yml:docker-compose.legacy-http.yml:docker-compose.local-build.yml",
        "DEPLOYMENT_MODE": "legacy-http",
        "RPA_SERVER_NAME": server_ip,
        "CASDOOR_SERVER_NAME": server_ip,
        "RPA_HTTP_BIND_ADDRESS": "0.0.0.0",
        "RPA_HTTP_PORT": "32742",
        "CASDOOR_HTTP_BIND_ADDRESS": "0.0.0.0",
        "CASDOOR_HTTP_PORT": "8000",
        "CASDOOR_EXTERNAL_ENDPOINT": f"http://{server_ip}:8000",
        "LEGACY_CASDOOR_EXTERNAL_ENDPOINT": f"http://{server_ip}:8000",
        "SESSION_COOKIE_SECURE": "false",
    }
    if not existing:
        password = secrets.token_hex(24)
        values.update(
            {
                "DATABASE_PASSWORD": password,
                "CASDOOR_DATABASE_PASSWORD": password,
                "ATLAS_URL": f"mysql://root:{password}@rpa-opensource-mysql:3306/rpa?charset=utf8mb4&parseTime=True",
                "ATLAS_HOST_URL": f"mysql://root:{password}@localhost:3306/rpa?charset=utf8mb4&parseTime=True",
                "MINIO_AK": "rpa" + secrets.token_hex(8),
                "MINIO_SK": secrets.token_hex(24),
            }
        )
    # Dedicated private readiness credential, never printed or passed as a CLI argument.
    if not re.search(r"^MONITORING_TOKEN[ \t]*=[ \t]*['\"]?[A-Za-z0-9_-]{32,}", content, re.MULTILINE):
        values["MONITORING_TOKEN"] = secrets.token_hex(24)
    content = credentials.update_env(content, values)
    if existing:
        required = (
            "DATABASE_PASSWORD",
            "CASDOOR_DATABASE_PASSWORD",
            "MINIO_AK",
            "MINIO_SK",
        )
        for key in required:
            if not re.search(r"^" + key + r"[ \t]*=[ \t]*['\"]?[^'\"\s]+", content, re.MULTILINE):
                raise ValueError("已有配置缺少必要凭据：" + key)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=ROOT,
            prefix=".http-deploy-",
            delete=False,
        ) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    print("HTTP 配置已就绪；凭据已保存在 .env，未显示。")


def databases(server_ip):
    ipaddress.IPv4Address(server_ip)
    config = json.loads(compose("config", "--format", "json"))
    if config.get("name") != "astron-rpa":
        raise ValueError("拒绝对非 astron-rpa 项目执行建库")
    environment = config["services"]["rpa-auth"]["environment"]
    names = (environment["DATABASE_NAME"], environment["CASDOOR_DATABASE_NAME"])
    if any(not re.fullmatch(r"[A-Za-z0-9_]+", name) for name in names):
        raise ValueError("数据库名称只允许字母、数字和下划线")
    query = "\n".join(
        f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
        for name in dict.fromkeys(names)
    )
    compose(
        "exec",
        "-T",
        "mysql",
        "sh",
        "-c",
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root --batch',
        data=query,
    )

    def sql(query):
        return compose(
            "exec",
            "-T",
            "mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root --batch --skip-column-names --default-character-set=utf8mb4',
            data=query,
        ).strip()

    if names != ("rpa", "casdoor"):
        raise ValueError("当前初始化 SQL 使用 rpa/casdoor 库名，请勿自定义库名后自动导入")
    count = int(sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='rpa';"))
    if count == 0:
        sql((ROOT / "volumes/mysql/schema.sql").read_text(encoding="utf-8"))
        print("空业务库的初始表结构已导入。")
    seeds = {
        "app_market_dict": "init_app_market_dict_data.sql",
        "his_data_enum": "init_his_data_enum_data.sql",
        "sample_templates": "init_sample_template_data.sql",
        "c_atom_meta_new": "init_c_atom_meta_new_data.sql",
    }
    for table, filename in seeds.items():
        if int(sql(f"SELECT COUNT(*) FROM rpa.`{table}`;")) == 0:
            sql("USE rpa;\n" + (ROOT / "volumes/mysql" / filename).read_text(encoding="utf-8"))
            print("已填充空的初始化表：" + table)
    print("RPA 和 Casdoor 数据库已确认存在；未删除数据或重设密码。")


def materials(server_ip):
    ipaddress.IPv4Address(server_ip)
    # These are public checked-in bootstrap assets, never deployment secrets/data.
    for directory in ("mysql", "nginx", "atlas", "casdoor"):
        root = ROOT / "volumes" / directory
        if root.is_symlink():
            raise ValueError("配置目录不能是符号链接：" + directory)
        root.chmod(0o755)
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("配置文件不能是符号链接")
            if path.is_dir():
                path.chmod(0o755)
            elif path.suffix in (
                ".sql",
                ".cnf",
                ".conf",
                ".lua",
                ".sh",
                ".template",
                ".json",
                ".hcl",
            ):
                path.chmod(0o644)
    print("本项目公开初始化配置权限已修正；未改动 .env、数据目录或其他项目。")


def request(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=4) as response:
            return response.status, response.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(65536).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("HTTP 无法连接：" + url) from None


def guard(server_ip):
    """Refuse to adopt containers managed by another Compose project."""
    ipaddress.IPv4Address(server_ip)
    config = json.loads(compose("config", "--format", "json"))
    if config.get("name") != "astron-rpa":
        raise ValueError("Compose 项目名必须为 astron-rpa，拒绝使用可能与其他服务冲突的项目名")
    for service in config["services"].values():
        name = service.get("container_name")
        if not name:
            continue
        result = subprocess.run(
            ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if result.returncode:
            raise ValueError("无法确认容器归属，已停止部署")
        if not result.stdout.strip():
            continue
        result = subprocess.run(
            ["docker", "inspect", name],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if result.returncode:
            raise ValueError("无法检查已有容器 " + name)
        labels = json.loads(result.stdout)[0].get("Config", {}).get("Labels", {}) or {}
        if labels.get("com.docker.compose.project") != "astron-rpa":
            raise ValueError("容器 " + name + " 归属其他项目；请先保留数据并完成 RPA 迁移，禁止直接接管或清理其他项目")
        working_dir = labels.get("com.docker.compose.project.working_dir")
        if working_dir and Path(working_dir).resolve() != ROOT.resolve():
            raise ValueError("容器 " + name + " 来自另一部署目录，已停止部署")
    print("项目隔离检查通过：astron-rpa；未执行容器接管或清理。")


# Runs inside openapi-service, so private service ports need no public mapping.
INTERNAL_CHECK = r"""
import json, os, sys, urllib.request, urllib.error
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
checks = [
    ("ai-service", "http://ai-service:8010/", "json"),
    ("openapi-service", "http://127.0.0.1:8020/health/ready", "ready"),
    ("robot-service", "http://robot-service:8040/api/robot/", "java"),
    ("resource-service", "http://resource-service:8030/api/resource/", "java"),
]
for name, url, kind in checks:
    headers = {"Authorization": "Bearer " + os.environ.get("MONITORING_TOKEN", "")} if kind == "ready" else {}
    try:
        try:
            response = opener.open(urllib.request.Request(url, headers=headers), timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        code = response.code
        body = response.read(65536)
        if kind == "java":
            assert code in (200, 401, 403, 404)
        else:
            assert code == 200
            data = json.loads(body)
            assert data.get("ready") is True if kind == "ready" else "message" in data
    except Exception:
        print(name + " 内部 HTTP/依赖就绪检查失败")
        sys.exit(1)
print("内部服务检查通过")
"""


def verify(server_ip):
    ipaddress.IPv4Address(server_ip)
    config = json.loads(compose("config", "--format", "json"))
    if config["services"]["openresty-nginx"]["environment"]["DEPLOYMENT_MODE"] != "legacy-http":
        raise ValueError("网关未启用 HTTP 模式")
    for name in SERVICES:
        ids = compose("ps", "--all", "--quiet", name).split()
        if len(ids) != 1:
            raise ValueError(name + " 容器不存在或数量异常")
        result = subprocess.run(
            ["docker", "inspect", ids[0]],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if result.returncode:
            raise ValueError(name + " 容器状态读取失败")
        state = json.loads(result.stdout)[0]["State"]
        if state.get("Status") != "running" or state.get("Restarting"):
            raise ValueError(name + " 未运行或正在重启")
        if state.get("Health", {}).get("Status", "healthy") != "healthy":
            raise ValueError(name + " 健康检查尚未通过")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "openapi-service",
            "python",
            "-c",
            INTERNAL_CHECK,
        ],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    if result.returncode:
        # Only output known messages from our checker, never container stderr.
        for name in (
            "ai-service",
            "openapi-service",
            "robot-service",
            "resource-service",
        ):
            if name + " 内部 HTTP/依赖就绪检查失败" in result.stdout:
                raise ValueError(name + " 内部 HTTP/依赖就绪检查失败")
        raise ValueError("内部服务检查命令执行失败")
    for host in dict.fromkeys(("127.0.0.1", server_ip)):
        base = f"http://{host}:32742"
        code, body = request(base + "/health")
        if code != 200 or body.strip() != "healthy":
            raise ValueError(f"网关检查失败：{host}:32742，HTTP {code}")
        code, body = request(base + "/api/rpa-auth/user/login-check")
        try:
            valid = code in (200, 401) and str(json.loads(body).get("code")) == "900001"
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError(f"认证 API 未返回预期未登录响应：{host}:32742，HTTP {code}")
        code, body = request(f"http://{host}:8000/")
        if code != 200 or "<html" not in body.lower():
            raise ValueError(f"Casdoor 网站尚不可用：{host}:8000，HTTP {code}")
    print("全部服务检查通过")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "guard", "materials", "databases", "verify"))
    parser.add_argument("--server-ip", required=True)
    args = parser.parse_args()
    try:
        {
            "prepare": prepare,
            "guard": guard,
            "materials": materials,
            "databases": databases,
            "verify": verify,
        }[args.action](args.server_ip)
    except ValueError as error:
        print(str(error))
        return 1
    except (OSError, subprocess.SubprocessError, KeyError, TypeError):
        print("部署检查无法完成，请检查文件权限、Compose 配置和 Docker 连接")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
