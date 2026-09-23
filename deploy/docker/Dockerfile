# =============================================================================
# OpsManager 后端镜像（多阶段：builder 编译 → runtime 只带 .so）
#
# 构建（仓库根目录）：
#   make image                         # 推荐，会带上 tag 与 BUILD_INFO
#   docker build -f deploy/docker/Dockerfile -t ops-manager-backend:dev .
#
# ── 基线为什么是 ubuntu:24.04 而不是 python:3.14-slim ────────────────────────
# 1. mysqlclient 链接宿主的 MySQL 客户端库：Ubuntu 的 default-libmysqlclient-dev
#    指向 MySQL（libmysqlclient.so.21），Debian 的指向 MariaDB（libmariadb.so.3）。
#    wheelhouse 流程（Dockerfile.builder）已经锁在 ubuntu:24.04，运行时不统一
#    过去就是两套基线。
# 2. Cython 产出的 .so 带 ABI tag（cpython-314-x86_64-linux-gnu）并链接 glibc，
#    编译与运行必须同 Python 主次版本、同 libc。base 阶段被 builder 和 runtime
#    共同继承，这个约束就由 Dockerfile 自身保证，不靠人记。
# =============================================================================

ARG UBUNTU_VERSION=24.04
ARG PYTHON_VERSION=3.14

# ── base：builder 与 runtime 的共同祖先，决定 Python / libc 基线 ─────────────
FROM ubuntu:${UBUNTU_VERSION} AS base
ARG PYTHON_VERSION
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# 运行时系统依赖：
#   libmysqlclient21  mysqlclient 的运行时库（唯一需要编译的依赖链接它）
#   openssh-client / sshpass / rsync  Ansible 连目标机、synchronize 模块
#   curl / openssl    acme.sh 签发证书；容器 healthcheck
#   git               Ansible 的 git 模块 / 部分方法拉取仓库
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates gnupg software-properties-common \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} \
        python${PYTHON_VERSION}-venv \
        libmysqlclient21 \
        openssh-client sshpass rsync \
        curl openssl git tzdata \
    && apt-get purge -y software-properties-common gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# ── builder：装依赖 + Cython 编译 + collectstatic ────────────────────────────
FROM base AS builder
ARG PYTHON_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION}-dev \
        build-essential pkg-config \
        default-libmysqlclient-dev libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# venv 而非系统 python：Ubuntu 的 python3.x 带 EXTERNALLY-MANAGED 标记，
# 直接 pip install 会被 PEP 668 拒绝
RUN python${PYTHON_VERSION} -m venv ${VENV} \
    && pip install -U pip setuptools wheel

WORKDIR /app

# 依赖层单独缓存：lock 未变时重建跳过 pip install。
# 按 requirements.lock 而非 requirements.txt 装：后者全是下界约束，
# 同一 commit 隔一周重建会装出不同版本的 Django / ansible-core。
COPY requirements.lock deploy/docker/requirements-build.txt ./
RUN pip install -r requirements.lock -r requirements-build.txt

COPY . .

# 1) Cython 编译业务代码并删源码（范围与排除见 deploy/cython/build.py）
RUN python deploy/cython/build.py --strip

# 2) 编译产物自检：能 django.setup() + check --deploy 才算编译成功。
#    没有这一步，annotation_typing / binding 之类的编译指令出错会拖到
#    生产第一个请求才暴露。
#    SECRET_KEY 等用 settings 里的默认值，check 不连数据库。
RUN DJANGO_SETTINGS_MODULE=ops_manager.settings.prod python manage.py check --deploy \
    && DJANGO_SETTINGS_MODULE=ops_manager.settings.prod python manage.py spectacular --file /dev/null \
    && echo '编译产物自检通过（app registry + URLConf + DRF 序列化器全部导入成功）'

# 3) 静态文件在构建期收好，运行时容器不需要写权限也不需要重复跑
RUN DJANGO_SETTINGS_MODULE=ops_manager.settings.prod python manage.py collectstatic --noinput

# 构建期垃圾不带进运行时镜像
RUN rm -rf requirements-build.txt deploy/cython .git \
    && find /app -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find ${VENV} -name '__pycache__' -type d -prune -exec rm -rf {} +

# ── runtime：无 gcc、无 python-dev、无 .py 业务源码 ──────────────────────────
FROM base AS runtime
ARG APP_UID=1000
ARG APP_GID=1000
ARG BUILD_TS=dev
ARG GIT_COMMIT=unknown

# ubuntu:24.04 自带 uid 1000 的 ubuntu 用户，先让位再按宿主的 uid/gid 建 app 用户。
# uid 必须与宿主上 logs/ media/ 目录的 owner 一致，否则 bind mount 写不进去。
RUN userdel -r ubuntu 2>/dev/null || true; \
    groupadd -g ${APP_GID} app 2>/dev/null || true; \
    useradd -m -u ${APP_UID} -g ${APP_GID} -s /bin/bash -d /home/app app

COPY --from=builder --chown=${APP_UID}:${APP_GID} ${VENV} ${VENV}
COPY --from=builder --chown=${APP_UID}:${APP_GID} /app /app

WORKDIR /app

# logs / media / run 都是 bind mount 点，先建好并交给 app 用户：
# 宿主目录若由 deploy/docker/init-dirs.sh 建好则以宿主 owner 为准，
# 这里兜的是「忘了跑 init-dirs.sh」的情况。
RUN mkdir -p /app/logs /app/media /app/run \
    && chown -R ${APP_UID}:${APP_GID} /app/logs /app/media /app/run /home/app

ENV DJANGO_SETTINGS_MODULE=ops_manager.settings.prod \
    HOME=/home/app \
    BUILD_TS=${BUILD_TS} \
    GIT_COMMIT=${GIT_COMMIT}

LABEL com.opsmanager.commit="${GIT_COMMIT}" \
      com.opsmanager.build-ts="${BUILD_TS}"

USER app

EXPOSE 8000 8001
ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
CMD ["web"]
