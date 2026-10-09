#!/usr/bin/env bash
# OpsManager 制品安装脚本（在目标服务器上、解包后的发布目录里执行）
#
#   tar -xzf opsmgr-<BUILD_TS>.tar.gz -C /home/ubuntu/apps/devops-manager-01
#   cd /home/ubuntu/apps/devops-manager-01
#   bash deploy/install.sh
#
# 与 deploy.sh / update.sh 的分工：
#   deploy.sh  首次从源码树部署（开发机 / 无 CI 场景，联网装依赖）
#   update.sh  从 git 拉代码更新（无 CI 场景）
#   install.sh 【CD 主路径】装 Jenkins 制品：离线、不编译、不构建前端
#
# 本脚本【不联网】：依赖全部来自产物内的 wheelhouse/，静态文件与前端 dist
# 已在 CI 侧生成好。目标机唯一的系统前置是 python3.14 与 libmysqlclient21。
set -euo pipefail
cd "$(dirname "$0")/.."
RELEASE_DIR="$PWD"

export DJANGO_SETTINGS_MODULE=ops_manager.settings.prod

VENV=${VENV:-venv}
WHEELHOUSE=${WHEELHOUSE:-wheelhouse}
# 程序名与 deploy/opsmgr.conf 的 [program:xxx] 一致；多实例部署时覆盖
SUPERVISOR_PROGRAM=${SUPERVISOR_PROGRAM:-opsmgr-01}
# 现网用的是自封装的 supervisor 包装脚本，不是 systemd（见 deploy/README.md）
MANAGER_BIN=${MANAGER_BIN:-/home/ubuntu/scripts/manager.sh}

DO_MIGRATE=true
DO_RESTART=true
for arg in "$@"; do
    case "$arg" in
        --deps-only)  DO_MIGRATE=false; DO_RESTART=false ;;
        --no-migrate) DO_MIGRATE=false ;;
        --no-restart) DO_RESTART=false ;;
        -h|--help)
            sed -n '2,14p' "$0"; exit 0 ;;
        *)
            echo "未知参数：$arg（可用：--deps-only --no-migrate --no-restart）" >&2
            exit 2 ;;
    esac
done

die() { echo "✗ $*" >&2; exit 1; }

# ── 前置校验：把能在装之前发现的问题全在这里报掉 ──────────────────────────
[ -d "$WHEELHOUSE" ] || die "缺少 $WHEELHOUSE/：这不是 CI 产物包，源码树部署请用 deploy/deploy.sh"
[ -f requirements.lock ] || die "缺少 requirements.lock"
[ -f .env ] || die "缺少 .env：cp deploy/env.example .env 并按实际环境修改后重试"

# 产物里的轮子是按构建基线的 Python 主次版本编的，装到别的小版本上会因
# ABI tag 不匹配而被 pip 判为「无可用发行版」（离线时报错很不直观），先明确拦掉
PY_BASELINE=$(sed -n 's/^python_baseline=//p' BUILD_INFO 2>/dev/null || true)
PY_BASELINE=${PY_BASELINE:-3.14}
PYTHON_BIN=${PYTHON_BIN:-python${PY_BASELINE}}
command -v "$PYTHON_BIN" >/dev/null \
    || die "找不到 $PYTHON_BIN。产物按 Python $PY_BASELINE 构建，目标机须装同一主次版本（PYTHON_BIN 可覆盖）"

echo "==> 发布目录：$RELEASE_DIR"
sed 's/^/    /' BUILD_INFO 2>/dev/null || true

WHEEL_COUNT=$(find "$WHEELHOUSE" -maxdepth 1 -name '*.whl' | wc -l)
[ "$WHEEL_COUNT" -gt 0 ] || die "$WHEELHOUSE/ 里没有 .whl，产物不完整"

# ── venv 每次重建：离线装只需几秒，可避免上一版残留的依赖留在环境里 ────────
#
# 旧 venv 先挪到 $VENV.old 而不是直接删：装依赖或自检失败时原样挪回，正在跑的
# 服务不受影响（gunicorn 进程已把 .so 映射进内存，目录被挪走也能继续跑到重启）。
# 不用「先建 $VENV.new 再 mv 成 $VENV」的写法：venv 不可重定位，bin/ 下所有脚本
# 的 shebang 会硬编成 $VENV.new 的绝对路径，mv 之后全部指向不存在的解释器。
VENV_BACKUP="$VENV.old"

rollback_and_die() {
    rm -rf "$VENV"
    if [ -d "$VENV_BACKUP" ]; then
        mv "$VENV_BACKUP" "$VENV"
        echo "已回滚到上一版虚拟环境（服务未重启，仍在旧版本上运行）" >&2
    fi
    die "$@"
}

echo "==> 重建虚拟环境并离线安装依赖（$WHEEL_COUNT 个轮子）"
rm -rf "$VENV_BACKUP"
if [ -d "$VENV" ]; then
    mv "$VENV" "$VENV_BACKUP"
fi
"$PYTHON_BIN" -m venv "$VENV" || rollback_and_die "创建虚拟环境失败（$PYTHON_BIN -m venv）"
# --no-index：即使目标机能上网也绝不去 PyPI，保证装出来的与制品声明逐一相同。
# venv 自带的 pip 不升级——升级就得联网，且装轮子不需要新版 pip。
"$VENV/bin/pip" install --no-index --find-links="$WHEELHOUSE" -r requirements.lock \
    || rollback_and_die "离线安装依赖失败：wheelhouse 与 requirements.lock 不匹配，或 Python 版本与构建基线不同"

# ── 依赖自检：mysqlclient 是唯一的 C 扩展，soname 对不上只在 import 时才炸，
#    留到 gunicorn 启动时暴露会变成「服务起不来但日志在别处」，这里提前引爆 ──
echo "==> 依赖自检"
IMPORT_LOG=$(mktemp)
if ! "$VENV/bin/python" -c 'import MySQLdb, django, ansible' 2>"$IMPORT_LOG"; then
    cat "$IMPORT_LOG" >&2
    if grep -q 'libmysqlclient' "$IMPORT_LOG"; then
        rollback_and_die "缺少 mysqlclient 的运行时库，执行：sudo apt-get install -y libmysqlclient21"
    fi
    if grep -q 'libmariadb' "$IMPORT_LOG"; then
        rollback_and_die "wheelhouse 是在 Debian 基线上编的（链接 libmariadb），与本机 Ubuntu 不匹配：用 deploy/docker/Dockerfile.builder 重新 make wheels"
    fi
    rollback_and_die "依赖自检失败，详见上方 traceback"
fi
rm -f "$IMPORT_LOG"
"$VENV/bin/python" -c 'import django; print("    django", django.get_version())'
# 保留 $VENV.old 供人工回滚（把它挪回来 + 重启即可），下次 install.sh 会清掉

# ── collectstatic 不在这里跑：staticfiles/ 已随产物走 ──────────────────────
if [ -d staticfiles ]; then
    echo "==> 静态文件随产物提供（$(find staticfiles -type f | wc -l) 个文件），跳过 collectstatic"
else
    echo "⚠ 产物内无 staticfiles/，nginx 将取不到 admin/DRF 静态资源" >&2
fi
if [ -d frontend/dist ]; then
    echo "==> 前端产物随包提供，跳过 npm 构建"
else
    echo "⚠ 产物内无 frontend/dist/（构建时 SKIP_FRONTEND=true？）" >&2
fi

mkdir -p logs media

if [ "$DO_MIGRATE" = true ]; then
    echo "==> 数据库迁移"
    "$VENV/bin/python" manage.py migrate --noinput
else
    echo "==> 跳过数据库迁移（--no-migrate）"
fi

if [ "$DO_RESTART" = true ]; then
    echo "==> 重启 $SUPERVISOR_PROGRAM"
    if [ -x "$MANAGER_BIN" ]; then
        "$MANAGER_BIN" restart "$SUPERVISOR_PROGRAM"
    elif command -v supervisorctl >/dev/null; then
        supervisorctl restart "$SUPERVISOR_PROGRAM"
    else
        die "找不到 $MANAGER_BIN 也找不到 supervisorctl，请手工重启 $SUPERVISOR_PROGRAM"
    fi
else
    echo "==> 跳过重启（--no-restart）"
fi

echo "安装完成：$(sed -n 's/^commit=//p' BUILD_INFO 2>/dev/null || echo unknown)"
