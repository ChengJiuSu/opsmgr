# OpsManager 后端镜像：Django + gunicorn + ansible（任务执行）
# 构建（仓库根目录）：docker compose build
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=ops_manager.settings.prod

# gcc/pkg-config/libmysqlclient：编译 mysqlclient；
# openssh-client/sshpass：Ansible SSH 连接目标主机
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc pkg-config default-libmysqlclient-dev \
        openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层单独缓存：requirements 未变化时重建跳过 pip install
COPY requirements.txt .
RUN pip install -r requirements.txt gunicorn

COPY . .

RUN mkdir -p logs media staticfiles

EXPOSE 8000
ENTRYPOINT ["bash", "deploy/docker/entrypoint.sh"]
