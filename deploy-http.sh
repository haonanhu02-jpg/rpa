#!/usr/bin/env bash
# Linux: bash deploy-http.sh; override APP_DIR / SERVER_IP if needed.
set -Eeuo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/astron-rpa}"
SERVER_IP="${SERVER_IP:-192.168.3.110}"
BRANCH="${BRANCH:-main}"
GITHUB_URL="https://github.com/haonanhu02-jpg/rpa.git"
# Mirror first, matching the deployment workflow used on this server.
GIT_MIRROR_URL="${GIT_MIRROR_URL:-https://ghfast.top/https://github.com/haonanhu02-jpg/rpa.git}"
GIT_FETCH_TIMEOUT="${GIT_FETCH_TIMEOUT:-300}"
DEPLOY_TIMEOUT="${DEPLOY_TIMEOUT:-300}"
export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=never
STAGE="环境检查"
REASON="请检查 Bash、Git、Python 3、Docker 和 Docker Compose 是否可用。"
LOG_FILE=$(mktemp "${TMPDIR:-/tmp}/astron-rpa-deploy.XXXXXXXX.log")

on_error() {
    local code="$1"
    trap - ERR
    printf '\n部署失败（退出码 %s）\n失败阶段：%s\n原因：%s\n' "$code" "$STAGE" "$REASON" >&2
    if grep -Eqi 'manifest unknown|manifest.*not found|pull access denied|no matching manifest' "$LOG_FILE"; then
        echo '检测到镜像不存在、无权限或架构不匹配；请检查日志中的镜像名称和标签。' >&2
    elif grep -Eqi 'no space left|not enough space' "$LOG_FILE"; then
        echo '检测到磁盘空间不足。' >&2
    elif grep -Eqi 'address already in use|port is already allocated' "$LOG_FILE"; then
        echo '检测到端口占用，请检查 32742、8000 以及已有同名容器。' >&2
    elif grep -Eqi 'Access denied for user' "$LOG_FILE"; then
        echo '检测到数据库拒绝认证；已有数据库密码不会随 .env 自动修改。' >&2
    elif grep -Eqi 'Could not resolve|Temporary failure in name resolution|TLS handshake timeout|connection timed out' "$LOG_FILE"; then
        echo '检测到 DNS 或网络连接错误，请检查服务器到代码仓库、镜像仓库和依赖源的连接。' >&2
    fi
    printf '详细日志（可能包含配置，请勿公开）：%s\n' "$LOG_FILE" >&2
    echo '未执行容器 down 或数据卷删除；失败可能已留下部分更新，请排查后重新运行。' >&2
    exit "$code"
}
trap 'on_error "$?"' ERR
trap 'REASON="部署收到中断信号，请检查当前容器状态后重试。"; on_error 130' INT
trap 'REASON="部署收到终止信号，请检查当前容器状态后重试。"; on_error 143' TERM
printf '部署日志：%s（权限仅限当前用户）\n' "$LOG_FILE"

fail() { REASON="$1"; on_error 1; }
step() {
    STAGE="$1"
    REASON="$2"
    shift 2
    printf '\n>>> %s\n' "$STAGE"
    "$@" >>"$LOG_FILE" 2>&1
}

for tool in git python3 docker flock timeout tee; do
    command -v "$tool" >/dev/null || fail "缺少命令 $tool，请先安装。"
done
[[ "$APP_DIR" = /* && "$APP_DIR" != / ]] || fail 'APP_DIR 必须是绝对目录，且不能为 /。'
[[ "$DEPLOY_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail 'DEPLOY_TIMEOUT 必须为正整数秒数。'
[[ "$GIT_FETCH_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail 'GIT_FETCH_TIMEOUT 必须为正整数秒数。'
python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$SERVER_IP" >>"$LOG_FILE" 2>&1 || fail 'SERVER_IP 必须是有效 IPv4 地址。'
COMPOSE_VERSION=$(docker compose version --short 2>>"$LOG_FILE")
python3 -c 'import re,sys; v=tuple(map(int,re.search(r"(\d+)\.(\d+)\.(\d+)",sys.argv[1]).groups())); sys.exit(v < (2,24,4))' "$COMPOSE_VERSION" >>"$LOG_FILE" 2>&1 || fail '需要 Docker Compose 2.24.4 或更新版本。'
step '检查 Docker 引擎' 'Docker 未启动、当前用户无权限，或引擎不可连接。' docker info
[[ $(docker info --format '{{.OSType}}' 2>>"$LOG_FILE") = linux ]] || fail '需要运行 Linux 容器的 Docker 引擎。'
mkdir -p -- "$(dirname -- "$APP_DIR")"
exec 9>"${APP_DIR}.deploy.lock"
flock -n 9 || fail '该目录已有部署正在执行，请等待其结束。'

STAGE='获取项目代码'
REASON='镜像重试 3 次及 GitHub 原站回退均失败，请检查网络和仓库访问权限；现有容器尚未重建。'
GIT_OPTIONS=(-c http.version=HTTP/1.1 -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=60 -c "safe.directory=$APP_DIR")
git_local() { git "${GIT_OPTIONS[@]}" "$@"; }
if [[ -d "$APP_DIR/.git" ]]; then
    [[ -z $(git_local -C "$APP_DIR" status --porcelain) ]] || fail '项目目录有未提交修改，请先保存或提交；脚本不会覆盖这些文件。'
    [[ $(git_local -C "$APP_DIR" branch --show-current) = "$BRANCH" ]] || fail "当前分支不是 $BRANCH，请先切换到部署分支。"
else
    if [[ -e "$APP_DIR" ]]; then
        [[ -d "$APP_DIR" && -z $(ls -A -- "$APP_DIR") ]] || fail 'APP_DIR 非空且不是 Git 仓库，请改用空闲目录或自行完成仓库初始化。'
    fi
    mkdir -p -- "$APP_DIR"
    git init -b "$BRANCH" "$APP_DIR" >>"$LOG_FILE" 2>&1
    git_local -C "$APP_DIR" remote add origin "$GITHUB_URL"
fi

FETCHED=false
for source_kind in 镜像 GitHub原站; do
    if [[ "$source_kind" = 镜像 ]]; then
        source_url="$GIT_MIRROR_URL"
        attempts=3
        echo '优先通过 GitHub 镜像获取代码。'
    else
        source_url="$GITHUB_URL"
        attempts=1
        echo '镜像不可用，回退 GitHub 原站。'
    fi
    for ((attempt=1; attempt<=attempts; attempt++)); do
        printf '%s拉取：第 %s/%s 次尝试（单次最多 %s 秒）\n' "$source_kind" "$attempt" "$attempts" "$GIT_FETCH_TIMEOUT"
        if timeout --kill-after=10s "${GIT_FETCH_TIMEOUT}s" \
            git "${GIT_OPTIONS[@]}" -C "$APP_DIR" fetch --progress --no-tags "$source_url" "$BRANCH" 2>&1 | tee -a "$LOG_FILE"; then
            if ! git_local -C "$APP_DIR" cat-file -e FETCH_HEAD:docker/scripts/project-images.py 2>>"$LOG_FILE"; then
                echo '该代码来源尚未同步项目镜像加速脚本，尝试下一来源。'
                break
            fi
            FETCHED=true
            break
        fi
        printf '%s第 %s 次拉取失败或超时。\n' "$source_kind" "$attempt"
        if (( attempt < attempts )); then sleep 3; fi
    done
    [[ "$FETCHED" = false ]] || break
done
[[ "$FETCHED" = true ]] || fail "$REASON"
if git_local -C "$APP_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
    step '更新项目代码' '分支存在冲突或分叉，无法快进更新；请先处理 Git 状态。' git_local -C "$APP_DIR" merge --ff-only FETCH_HEAD
else
    git_local -C "$APP_DIR" checkout -B "$BRANCH" FETCH_HEAD >>"$LOG_FILE" 2>&1
fi

cd "$APP_DIR/docker"
# Do not let inherited CLI settings accidentally select another deployment.
unset COMPOSE_FILE COMPOSE_PATH_SEPARATOR
export COMPOSE_PROJECT_NAME=astron-rpa
HELPER="$APP_DIR/docker/scripts/http-deploy.py"
[[ -f "$HELPER" ]] || fail '仓库中缺少 docker/scripts/http-deploy.py，请确认已推送部署脚本。'
if [[ ! -e .env ]]; then
    STAGE='保护已有数据库'
    REASON='检测到旧部署但缺少 .env；请恢复原配置，避免给已有数据库生成不匹配的新密码。'
    OLD_CONTAINER=$(docker ps -aq --filter 'name=^/rpa-opensource-mysql$' 2>>"$LOG_FILE")
    OLD_VOLUMES=$(docker volume ls -q --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME:-docker}" --filter 'label=com.docker.compose.volume=mysql_data' 2>>"$LOG_FILE")
    [[ -z "$OLD_CONTAINER$OLD_VOLUMES" ]] || fail "$REASON"
fi
step '准备 HTTP 配置' '配置生成失败；已有 .env 必须包含有效的数据库和 MinIO 凭据，且不能是符号链接。' python3 "$HELPER" prepare --server-ip "$SERVER_IP"
step '校验 Compose' 'Compose 配置无效，请检查 .env 和 Compose 版本。' docker compose config --quiet
STAGE='检查部署项目隔离'
REASON='检测到容器项目归属冲突；已停止部署，未重建任何服务。'
echo ">>> $STAGE"
python3 "$HELPER" guard --server-ip "$SERVER_IP" 2>&1 | tee -a "$LOG_FILE"
step '修正本项目只读配置权限' '初始化配置权限修正失败。' python3 "$HELPER" materials --server-ip "$SERVER_IP"
# Scope builder selection and mirror cache to this deployment process/project.
export BUILDX_BUILDER=default
export DOCKER_BUILDKIT=1
STAGE='通过加速站准备本项目 Docker 镜像'
REASON='项目镜像加速失败，请检查上方具体镜像、网络、标签、架构或 Buildx 错误。'
echo ">>> $STAGE（不修改服务器 Docker 配置）"
python3 "$APP_DIR/docker/scripts/project-images.py" 2>&1 | tee -a "$LOG_FILE"
step '校验项目镜像缓存配置' '生成的项目镜像缓存配置无效。' docker compose config --quiet
step '构建当前后端源码' '构建失败，请在私有日志中检查 Maven/Python 依赖下载、编译错误或磁盘空间。' docker compose build
step '启动数据库和基础服务' 'MySQL、Redis 或 MinIO 启动失败或健康检查超时。' docker compose up -d --wait --wait-timeout "$DEPLOY_TIMEOUT" mysql redis minio
step '确认业务和认证数据库' '无法创建或确认 RPA/Casdoor 数据库，请检查 MySQL 认证及初始化日志。' python3 "$HELPER" databases --server-ip "$SERVER_IP"
step '启动 Casdoor' 'Casdoor 启动失败，请检查其数据库连接和初始化日志。' docker compose up -d --wait --wait-timeout "$DEPLOY_TIMEOUT" casdoor

STAGE='同步 Casdoor 凭据'
REASON='Casdoor 初始化或凭据读取超时，请检查 MySQL/Casdoor 日志以及 .env 中的数据库密码。'
echo ">>> $STAGE"
SYNCED=false
DEADLINE=$((SECONDS + DEPLOY_TIMEOUT))
while (( SECONDS < DEADLINE )); do
    if python3 scripts/sync-casdoor-credentials.py >>"$LOG_FILE" 2>&1; then
        SYNCED=true
        break
    fi
    sleep 5
done
[[ "$SYNCED" = true ]] || fail "$REASON"
step '执行数据库迁移' 'Atlas 数据库迁移失败；业务服务未启动，请检查数据库连接或 schema 差异。' docker compose run --rm atlas
step '启动业务服务和网关' '容器创建失败，请检查端口、同名容器及启动配置。' docker compose up -d --no-build --no-deps ai-service openapi-service resource-service robot-service rpa-auth openresty-nginx

STAGE='验证服务可用性'
REASON='服务检查失败。'
echo ">>> $STAGE（最多等待 ${DEPLOY_TIMEOUT} 秒）"
DEADLINE=$((SECONDS + DEPLOY_TIMEOUT))
VERIFIED=false
while (( SECONDS < DEADLINE )); do
    if CHECK_RESULT=$(python3 "$HELPER" verify --server-ip "$SERVER_IP" 2>>"$LOG_FILE"); then
        VERIFIED=true
        break
    fi
    printf '%s\n' "$CHECK_RESULT" >>"$LOG_FILE"
    REASON="$CHECK_RESULT"
    sleep 5
done
[[ "$VERIFIED" = true ]] || fail "服务就绪检查超时：$REASON"

printf '\n部署成功：服务端容器、内部接口、数据库/Redis 就绪检查及 HTTP 入口均已通过。\n'
printf '认证管理网站：http://%s:8000/\n服务端检查链接：http://%s:32742/health\n客户端 remote_addr：http://%s:32742/\n' "$SERVER_IP" "$SERVER_IP" "$SERVER_IP"
echo '当前未托管 RPA Web 控制台，32742 端口首页返回 404；通过 Windows 客户端使用 RPA。'
echo '请修改 Casdoor 默认管理员密码。客户端登录和任务执行仍需实际验收。'
printf '凭据文件：%s/docker/.env\n部署日志：%s\n' "$APP_DIR" "$LOG_FILE"
