#!/usr/bin/env bash
# 容器入口：等待数据库 → 迁移 → collectstatic → gunicorn
set -euo pipefail
cd /app

# 等待数据库就绪（compose 内置 MySQL 首次初始化较慢），migrate 兼作连接探测
for i in $(seq 1 30); do
    if python manage.py migrate --noinput; then
        break
    fi
    if [ "$i" = 30 ]; then
        echo "数据库连接失败（DB_HOST=${DB_HOST:-127.0.0.1}），退出。" >&2
        exit 1
    fi
    echo "等待数据库就绪…（$i/30）"
    sleep 2
done

python manage.py collectstatic --noinput

# --timeout 600：任务执行 / Jenkins 日志同步为长请求，勿调小
exec gunicorn ops_manager.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-4}" \
    --timeout 600 \
    --access-logfile - \
    --error-logfile -
