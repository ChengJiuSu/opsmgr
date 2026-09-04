#!/usr/bin/env bash
# OpsManager 首次部署脚本（在目标服务器上、项目根目录执行）
#   git clone <repo> /opt/devops-manager && cd /opt/devops-manager
#   cp deploy/env.example .env && vim .env   # 先改好环境变量
#   bash deploy/deploy.sh
#
# 前置：python3.14、node22+/npm、MySQL 已建库（utf8mb4）、
#       ansible 目标主机 SSH 免密已配置、libmysqlclient21 与 pkg-config +
#       default-libmysqlclient-dev（本脚本联网现编 mysqlclient）。
#
# ⚠ 本脚本是【无 CI 场景】的源码树部署路径，会联网装依赖、现编 C 扩展、本机
#   构建前端。走 Jenkins 的正式 CD 请用 deploy/install.sh 装制品包（离线、
#   不编译、不装 node），两者不要混用同一个 venv。
set -euo pipefail
cd "$(dirname "$0")/.."

export DJANGO_SETTINGS_MODULE=ops_manager.settings.prod

if [ ! -f .env ]; then
    echo "缺少 .env：先 cp deploy/env.example .env 并修改后重试" >&2
    exit 1
fi

echo "==> 创建虚拟环境并安装后端依赖"
# 按 requirements.lock 装（gunicorn 已收进声明文件）。requirements.txt 全是
# 下界约束，直接装它会随执行时间解出不同版本，与 CI 产物不一致。
[ -d venv ] || "${PYTHON_BIN:-python3.14}" -m venv venv
venv/bin/pip install -U pip
venv/bin/pip install -r requirements.lock

echo "==> 数据库迁移"
venv/bin/python manage.py migrate --noinput

echo "==> 收集静态文件"
venv/bin/python manage.py collectstatic --noinput

echo "==> 构建前端"
(cd frontend && npm ci && npm run build)

mkdir -p logs

echo "==> 创建管理员账号（已存在可 Ctrl+C 跳过）"
venv/bin/python manage.py createsuperuser || true

cat <<'EOF'

部署完成。剩余手工步骤：
  1. sudo cp deploy/ops-manager.conf /home/ubuntu/apps/supervisor/conf.d
     （改路径/用户/程序名）/home/ubuntu/scripts/manager.sh update devops-manager-01
     另需 ws-ops-manager.conf（daphne）与两个 celery worker，见 deploy/README.md
  2. sudo cp deploy/nginx.conf /etc/nginx/conf.d/ops-manager.conf
     （改 server_name / 路径）sudo nginx -t && sudo systemctl reload nginx
  3. 浏览器登录，「系统配置」页维护 jenkins.url / jenkins.user / jenkins.api_token
日常更新用 deploy/update.sh。
EOF
