# 192.168.3.110 HTTP deployment

## 自动部署 / 更新（推荐）

在 `192.168.3.110` 上确认已有 Git、Python 3、Docker、Docker Compose
2.24.4 或更新版本，以及 `flock`（通常由 util-linux 提供）。以下下载步骤
还需要 curl，脚本使用 coreutils 的 timeout 和 tee。允许客户端访问 TCP 32742 和 8000。

```bash
if curl -fL --retry 3 --connect-timeout 10 --max-time 60 \
  https://ghfast.top/https://raw.githubusercontent.com/haonanhu02-jpg/rpa/main/deploy-http.sh \
  -o deploy-http.sh.new || \
curl -fL --retry 3 --connect-timeout 10 --max-time 60 \
  https://raw.githubusercontent.com/haonanhu02-jpg/rpa/main/deploy-http.sh \
  -o deploy-http.sh.new; then
  mv deploy-http.sh.new deploy-http.sh
  sudo bash deploy-http.sh
else
  echo '镜像和原站均下载失败，请检查网络；未执行部署。' >&2
fi
```

默认部署目录为 `/opt/astron-rpa`，默认分支为 `main`，服务端地址为
`192.168.3.110`。自定义目录时可运行：

```bash
sudo env APP_DIR=/home/wsgfai/rpa SERVER_IP=192.168.3.110 bash deploy-http.sh
```

重复运行同一命令即可更新。已有目录必须是对应分支的干净 Git 仓库；
脚本只允许快进更新，不会覆盖未提交文件。默认先使用
`https://ghfast.top/https://github.com/haonanhu02-jpg/rpa.git` 拉取代码，
失败重试至三次后，再回退 GitHub 原站一次。每次都会显示来源、尝试次数和
Git 下载进度，并同时保存到日志。镜像地址可用 `GIT_MIRROR_URL` 覆盖。
单次拉取默认最多 300 秒，可通过 `GIT_FETCH_TIMEOUT` 调整；连接持续低速
超过 60 秒时也会中止重试，避免一直无输出地等待。此镜像用于 GitHub 代码，
Docker 镜像采用下面的项目级加速流程。

### 本项目 Docker 镜像加速

默认通过 DaoCloud 的 `docker.m.daocloud.io` 拉取本项目的基础服务镜像，
以及 Dockerfile 中的 Maven、Java、Python 构建/运行镜像。每个加速来源尝试
两次，再回退原始仓库一次；实时显示下载进度。已下载完整的镜像会复用，
无需重新下载。加速站也可能限流或缺少某个版本，原站回退失败时会报告具体
镜像名称和 Docker 错误。参考 [DaoCloud 官方用法](https://github.com/DaoCloud/public-image-mirror#加速-docker)。

范围限定在本项目：不修改 `/etc/docker/daemon.json`，不重新加载或重启 Docker，
不改变客户端默认构建器，不覆盖原始镜像标签。新增标签使用
`astron-rpa-cache/<项目标识>/<镜像标识>:cached`，配置和临时 Dockerfile
位于 `docker/.local/accelerated/`（已被 Git 忽略），`.env` 自动引用生成的
Compose 覆盖文件。原始 Dockerfile 保持不变。

需要 Docker Buildx 的 `default` 构建器使用 `docker` 驱动，脚本仅通过当前
进程的 `BUILDX_BUILDER=default` 选择它，以使用本机已缓存的镜像。生成的
Dockerfile 使用引擎内置 frontend，避免额外下载 Dockerfile frontend 镜像。
这是本机架构的 Linux 部署流程，未提供跨架构构建。

构建器检查先运行 `docker buildx version`，再运行
`docker buildx inspect default` 并读取 `Driver:` 字段；`inspect` 不支持
`--format` 参数。插件缺失、构建器读取失败和驱动不匹配会分别报告，
不会因检查失败自动安装插件、创建构建器或重启 Docker。

可按需设置环境变量：

- `DOCKER_IMAGE_MIRRORS`：逗号分隔的 Docker Hub 加速站域名/路径，默认 `docker.m.daocloud.io`。
- `IMAGE_PULL_TIMEOUT`：单次镜像拉取超时，默认 600 秒。
- `REFRESH_IMAGES=1`：忽略已有缓存，重新从加速站检查和拉取镜像。

例如：

```bash
sudo env IMAGE_PULL_TIMEOUT=300 bash deploy-http.sh
```

Python 镜像安装使用从各服务 `uv.lock` 导出的 `requirements.lock`，固定版本
并验证 SHA256；优先使用清华 PyPI 镜像，失败后回退官方 PyPI。
可通过 `PYPI_INDEX_URL`、`PYPI_FALLBACK_INDEX_URL` 设置这两个镜像构建参数，
不修改服务器 pip 全局配置。更新 Python 依赖后，在服务目录执行
`uv export --frozen --no-dev --no-emit-project --format requirements.txt --output-file requirements.lock`。

三个 Java 服务的构建使用 `docker/maven/` 中的项目级 settings 和脚本，
将 Maven Central 的依赖及插件下载映射到阿里云公共仓库。失败后使用官方
Central 设置重试相同命令，保留 `opensource` profile 和已下载依赖。
现有私有仓库不会被镜像规则重定向；不会修改服务器 `~/.m2/settings.xml`。
如需其他镜像，可设置构建参数 `MAVEN_MIRROR_URL`（仅 HTTPS）。
日志会显示 `astron-rpa-aliyun` 及 `maven.aliyun.com`；回退时明确显示提示。
重新构建会使受配置影响的 Java 构建层重新执行。镜像不可用或真实编译错误
导致两个尝试均失败时，构建仍会返回失败，不会误报成功。
参考 [Maven 官方镜像说明](https://maven.apache.org/guides/mini/guide-mirror-settings)。

这个加速范围不包括容器内的 apt 依赖下载；如果这些步骤失败，
脚本会在构建阶段报告失败并保留私有日志。镜像缓存、应用镜像和构建缓存仍
占用本机磁盘空间。

首次部署缺少 `.env` 时，脚本自动生成数据库和 MinIO 凭据，以及私有就绪
检查 token；已有 `.env` 会保留原密码和认证证书，并更新 HTTP 地址。
如果检测到已有数据库容器或当前 Compose 项目的数据卷却缺少 `.env`，
脚本会停止，要求恢复原配置，不会为旧数据库重新生成密码。
Compose 项目名固定为 `astron-rpa`，避免与 Dify 等同样使用 `docker/` 目录的
服务共享项目名。启动前检查固定容器名的归属：发现旧 `docker` 项目下的 RPA
容器时会停止，必须保留原数据库卷和 `.env` 并单独迁移，不能直接重新部署。
不得对共享的 `docker` 项目执行 `down` 或 `--remove-orphans`。
旧脚本曾以 `docker` 项目部署的主机，需要先确认并恢复被误重建的原项目 Redis；
不要根据 RPA 配置操作其他项目的容器。

针对旧脚本已与 Dify 共用 `docker` 项目名、MySQL 报 x86-64-v2 错误的部署，
提供 `python3 scripts/recover-shared-project.py` 只读预检，确认后加 `--apply`。
它核对四个 RPA 容器的标签和来源目录、Dify 原 Compose 文件及 MySQL 数据卷。
仅当 MySQL 卷为空时，才允许改用已存在的官方 `mysql:8.0.43-debian` 标签；
非空卷会停止，绝不降级已有 8.4 数据库。应用前先实际运行兼容镜像的
`--version`，确认此服务器 CPU 可以执行，再备份 `.env` 并保留原 MySQL 卷。
随后仅移除四个失败的 RPA 容器（不删卷），使用 Dify 容器记录的原配置恢复
Dify 的 `redis` 单个服务，不操作其他 Dify 服务，之后再以独立项目重新部署 RPA。

Redis 已改用官方 `redis:7.4.7-alpine` 的原生入口及 `/data` 挂载，避免 Bitnami
手工重复 setup/chown 引起的配置权限问题。MySQL 使用官方原生入口执行首次
初始化 SQL，不再在后台重复执行初始化脚本；已有部署的后续数据迁移需显式执行。

流程依次为：获取代码、准备配置、通过加速站准备本项目镜像、构建当前源码、启动基础
服务、幂等确认 RPA/Casdoor 数据库存在、启动 Casdoor 并同步凭据、执行 Atlas
迁移、启动业务服务、验证入口。显式建库步骤也适用于已有 MySQL 数据卷：
不会依赖只在空数据目录运行一次的初始化 SQL，不会删除数据库或重设密码。
启动前仅将项目公开初始化文件调整为 0644、相关目录调整为 0755，以允许
容器中的 mysql/nginx 用户读取；`.env`、数据目录和其他项目不在修改范围内。
如果首次 SQL 因权限问题未执行，脚本在业务库完全没有表时导入初始结构，
对四个初始化数据表仅在其为空时填充，已有记录不会被 TRUNCATE 覆盖。
Atlas 目标结构包含 OpenAPI 执行管理字段、幂等唯一索引和 utf8mb4 MEDIUMTEXT
数据列，并保留独立的执行历史；迁移前停止本项目 OpenAPI，迁移成功后再启动。
资源服务 HTTP 检查使用 `/api/resource/file/download`，不传 fileId 并严格校验
预期的 HTTP 400、业务码 500001；不会下载文件，也不会将 HTTP 500 视为成功。
该检查确认控制器可响应，不代表已完成文件上传/下载的业务验证。
脚本不会运行 `docker compose down` 或删除数据卷。构建失败时不会进入
服务重建阶段；数据库迁移和容器重建阶段发生失败时，可能已经留下部分更新，
需排查后重新运行；脚本不提供自动回滚。

只有以下检查全部通过，才会打印“部署成功”：所有必需容器运行且已有健康
检查通过；AI/OpenAPI 内部接口可用；OpenAPI 数据库/Redis 就绪；Java 服务
内部 HTTP 可响应；本机及服务器 IP 上的网关、认证 API、Casdoor 页面均可访问。
默认每个等待阶段约 300 秒，可用 `DEPLOY_TIMEOUT` 调整；正在执行的单次
探测有独立超时，实际等待可能略长。检查只发起只读请求，不会启动 RPA 任务。

成功后输出：

- 认证管理网站：<http://192.168.3.110:8000/>
- 服务端检查链接：<http://192.168.3.110:32742/health>
- Windows 客户端 `remote_addr`：`http://192.168.3.110:32742/`

认证网站是 Casdoor，不是 RPA Web 控制台；当前 32742 端口首页返回 404。
客户端登录、上传下载和实际流程执行仍需人工验收，其他电脑的访问还取决于
网络和防火墙。请修改 Casdoor 默认管理员密码。

失败会以非零退出码结束，打印失败阶段、原因、可能的镜像/网络/磁盘/端口/
数据库认证问题，以及详细日志位置。日志由当前用户私有保存，可能包含
连接串或业务信息，请勿直接公开。脚本开始时即打印日志位置，可在另一个
终端查看构建进度。AI/OCR 外部服务仍需按功能补充提供商凭据。

## 手动部署

This guide is for a fresh installation with empty MySQL storage. Changing
`.env` does not rotate a password in an existing MySQL database. If this host
already has an installation, back up its data and migrate credentials first;
do not delete its volumes to apply this guide.

## Transfer the project

Copy the complete current project to the Linux server, for example to
`/opt/astron-rpa`. Include the prepared `docker/.env` (hidden files may be
omitted by upload tools), `docker/docker-compose.local-build.yml`, and
`backend/`. Do not replace the prepared `.env` with `.env.example`.
The build requires the bundled `backend/rpa-auth/lib/rpa-auth-stubs-1.0.0.jar`
and `backend/openapi-service/app/utils/rpawebsocket-1.0.7-py3-none-any.whl`.

Docker Compose 2.24.4 or newer and Python 3 are required. Maven, Java, and
Python service dependencies are installed inside the build containers.
The server must be able to reach the image registries, Maven repositories,
and Python package index used by the Dockerfiles.

## Validate and build

Run as an operator who can use Docker and write to this deployment directory:

```bash
cd /opt/astron-rpa/docker
chmod 600 .env
docker compose version
python3 --version
docker compose config --quiet
docker compose pull mysql redis minio casdoor atlas openresty-nginx
docker compose build
```

Proceed only when each step succeeds. The prepared `.env` selects the HTTP
override and local application builds automatically. It also keeps the Compose
file separator consistent across Windows and Linux.

## Initialize services

```bash
docker compose up -d --wait mysql redis minio casdoor
python3 scripts/sync-casdoor-credentials.py
```

If synchronization reports that initialization is incomplete, wait for Casdoor
initialization and retry the Python command. Do not start the application
services until synchronization succeeds. The script writes this installation's
generated credentials to `.env` without printing them.

Apply the schema in the foreground:

```bash
docker compose run --rm atlas
```

Proceed only after Atlas exits successfully. Start applications and the gateway
with dependencies already initialized:

```bash
docker compose up -d --no-build --no-deps ai-service openapi-service resource-service robot-service rpa-auth openresty-nginx
docker compose ps
```

These commands deploy the current checkout using local application images.
No TLS certificate is needed. The gateway publishes only TCP 32742 and 8000.

## Verify and connect

```bash
curl -f http://192.168.3.110:32742/health
curl -i http://192.168.3.110:32742/api/rpa-auth/user/login-check
```

The first endpoint should return `healthy`; it checks the gateway only. An
unauthenticated request to the second endpoint should return the application's
unauthorized response. Open `http://192.168.3.110:8000` to access Casdoor;
change the default administrator password and secure unused sample accounts.

Configure the Windows client in its installed `resources/conf.yaml`:

```yaml
remote_addr: http://192.168.3.110:32742/
skip_engine_start: false
```

Verify login, client online status, file upload/download, workflow execution,
and results. The gateway root currently returns 404; a browser-hosted Web
console requires separate frontend deployment. Optional AI/OCR services need
their provider credentials in `.env` before use.

## Logs and subsequent changes

```bash
docker compose logs --tail=100 ai-service openapi-service resource-service robot-service rpa-auth openresty-nginx
```

Logs may contain application data; review them before sharing. Avoid printing
the expanded `docker compose config`, which contains credentials.

After changing application source, rebuild the affected image and recreate its
service. After changing deployment environment settings, recreate the affected
containers so the settings take effect. Keep the prepared `.env` and persistent
MySQL and MinIO data backed up. Never run `docker compose down -v` to restart.
