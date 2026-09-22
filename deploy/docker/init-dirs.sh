#!/usr/bin/env bash
# =============================================================================
# 建好所有 bind mount 的宿主目录并设好属主。**首次 compose up 之前必须跑一次。**
#
#   sudo bash deploy/docker/init-dirs.sh
#
# 为什么必须：bind mount 的宿主目录由 docker 自动创建时属主是 root，
# 而容器里进程是降权跑的（app / mysql / redis 都不是 root），
# 结果是容器起来就报 Permission denied，且各家报错措辞不同、看着像三个问题。
#
# uid 约定：
#   app     APP_UID/APP_GID，默认 1000（宿主 homelab）——与镜像构建参数一致
#   mysql   999:999（mysql:8.0 官方镜像内的 mysql 用户）
#   redis   999:999（redis:7.0-alpine 内的 redis 用户）
#   nginx   目录留 root，nginx master 以 root 起再降权 worker
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/../.."
[ -f .env ] && set -a && . ./.env && set +a

OPS_DATA_ROOT="${OPS_DATA_ROOT:-/home/homelab/data/ops-manager}"
OPS_LOG_ROOT="${OPS_LOG_ROOT:-/home/homelab/logs/ops-manager}"
APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"
MYSQL_UID=999
REDIS_UID=999

echo "数据根目录：${OPS_DATA_ROOT}"
echo "日志根目录：${OPS_LOG_ROOT}"

mkdir -p \
    "${OPS_DATA_ROOT}"/{mysql,redis,media,beat,staticfiles} \
    "${OPS_LOG_ROOT}"/{app,mysql,redis,nginx}

# 应用侧：logs（Django file handler + gunicorn access + 任务日志）、
# media（制品与 ACME 私钥）、beat（celerybeat-schedule）、staticfiles（导出给 nginx）
chown -R "${APP_UID}:${APP_GID}" \
    "${OPS_LOG_ROOT}/app" \
    "${OPS_DATA_ROOT}/media" \
    "${OPS_DATA_ROOT}/beat" \
    "${OPS_DATA_ROOT}/staticfiles"

# media 下有 ACME 账号私钥与证书私钥，别给同组/其他人读
chmod 750 "${OPS_DATA_ROOT}/media"

chown -R "${MYSQL_UID}:${MYSQL_UID}" "${OPS_DATA_ROOT}/mysql" "${OPS_LOG_ROOT}/mysql"
chown -R "${REDIS_UID}:${REDIS_UID}" "${OPS_DATA_ROOT}/redis" "${OPS_LOG_ROOT}/redis"

echo "目录就绪。宿主 Vector 采集路径需要指到："
echo "  OPS_APP_DIR 对应的 logs/  ->  ${OPS_LOG_ROOT}/app"
echo "  OPS_LOG_DIR              ->  ${OPS_LOG_ROOT}/app（gunicorn-access.log 也在这里）"
echo "  nginx 日志               ->  ${OPS_LOG_ROOT}/nginx"
