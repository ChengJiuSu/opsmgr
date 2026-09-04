#!/usr/bin/env bash
# OpsManager 日常更新脚本（在目标服务器上、项目根目录执行）
#   bash deploy/update.sh
#
# ⚠ 这是【无 CI 场景】的 git 拉取更新路径：联网装依赖、本机构建前端。
#   走 Jenkins 的正式 CD 用 deploy/install.sh 装制品包（离线、不编译）。
set -euo pipefail
cd "$(dirname "$0")/.."

export DJANGO_SETTINGS_MODULE=ops_manager.settings.prod

echo "==> 拉取最新代码"
git pull --ff-only

echo "==> 更新后端依赖"
# 按锁文件装，保证和 CI 产物、和其他实例装出同一套版本
venv/bin/pip install -r requirements.lock

echo "==> 数据库迁移"
venv/bin/python manage.py migrate --noinput

echo "==> 收集静态文件"
venv/bin/python manage.py collectstatic --noinput

echo "==> 构建前端"
(cd frontend && npm ci && npm run build)

echo "==> 重启后端服务"
# supervisor 托管（deploy/ops-manager.conf）；程序名不同时以环境变量覆盖：
#   SUPERVISOR_PROGRAM=xxx bash deploy/update.sh
# 仍用 systemd（deploy/ops-manager.service）时改回：sudo systemctl restart ops-manager
/home/homelab/scripts/manager.sh restart all

echo "更新完成：$(git log -1 --oneline)"
