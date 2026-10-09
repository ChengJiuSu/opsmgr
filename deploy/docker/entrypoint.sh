#!/usr/bin/env bash
# =============================================================================
# 容器入口：按第一个参数（角色）决定跑哪个进程。
# 一个镜像六个角色，与 deploy/*.conf 里的 supervisor program 一一对应：
#
#   init          迁移 + 导出静态文件（一次性，其余服务 depends_on 它成功）
#   web           ops-manager           gunicorn  :8000
#   ws            ops-manager-ws        daphne    :8001
#   celery        ops-manager-celery        默认队列（证书签发/分发）
#   celery-exec   ops-manager-celery-exec   task_exec 队列（任务执行）
#   beat          ops-manager-celery-beat   定时调度，只能起一个实例
#   feishu-ws     ops-manager-feishu-ws     飞书长连接（与回调模式互斥）
#
# 其它参数原样执行，便于 `docker compose run --rm web python manage.py xxx`。
# =============================================================================
set -euo pipefail
cd /app

ROLE="${1:-web}"
shift || true

# 各角色的 LOG_SERVICE 必须互不相同：它既是结构化日志的 service 字段，
# 也是落盘文件名后缀（logs/ops_manager-<service>.log）。
# 撞名 = 多进程写同一文件，轮转时互相覆盖。
export LOG_SERVICE="${LOG_SERVICE:-$ROLE}"

wait_for_db() {
    # migrate 兼作连接探测：compose 的 mysql healthcheck 只保证 mysqld 起来了，
    # 首次初始化建库建用户还要再等一会。
    local i
    for i in $(seq 1 30); do
        if python manage.py migrate --noinput; then
            return 0
        fi
        if [ "$i" = 30 ]; then
            echo "数据库连接失败（DB_HOST=${DB_HOST:-127.0.0.1}），退出。" >&2
            exit 1
        fi
        echo "等待数据库就绪…（$i/30）"
        sleep 2
    done
}

case "$ROLE" in
init)
    wait_for_db
    # 静态文件在镜像构建期就 collectstatic 好了，这里只把它导出到宿主目录，
    # 供前置 nginx 直接发（nginx 以只读挂同一个宿主目录）。
    # 不直接把 /app/staticfiles 设成挂载点：那样宿主的空目录会盖住镜像内的产物。
    if [ -d /app/static-export ]; then
        rsync -a --delete /app/staticfiles/ /app/static-export/
        echo "静态文件已导出到 /app/static-export"
    fi
    echo "init 完成（commit=${GIT_COMMIT:-unknown} build=${BUILD_TS:-dev}）"
    ;;

web)
    # 参数与 deploy/opsmgr.conf 逐项对齐，改一边要改两边：
    #   gthread 4×8 + --preload：4C/3.7G 上单进程 RSS ≈ 116MB，不 preload 时
    #     16 个 worker 直接换页；preload 后 fork 前加载一次，靠 COW 共享。
    #     代价是不能 HUP 热重载，改代码必须重启容器（本来就是换镜像）。
    #   --timeout 600：任务执行 / Jenkins 日志同步是长请求，勿调小。
    #   access log 走文件而非 stdout：宿主 Vector 按文件采集（见 vector.yaml 的
    #     gunicorn source），改成 stdout 需要同步改采集端。
    exec gunicorn ops_manager.wsgi:application \
        --bind 0.0.0.0:8000 \
        --worker-class gthread \
        --workers "${GUNICORN_WORKERS:-4}" \
        --threads "${GUNICORN_THREADS:-8}" \
        --preload \
        --timeout "${GUNICORN_TIMEOUT:-600}" \
        --max-requests 2000 \
        --max-requests-jitter 200 \
        --access-logfile /app/logs/gunicorn-access.log \
        --access-logformat '{"ts_clf":"%(t)s","service":"gunicorn","method":"%(m)s","uri":"%(U)s","args":"%(q)s","status":"%(s)s","dur_ms":"%(M)s","pid":"%(p)s","rid":"%({x-request-id}i)s"}' \
        "$@"
    ;;

ws)
    # 承载 /ws/：执行日志实时推送 + ops-agent 接入。
    # 不部署它 /ws/ 就是 502，前端退回每秒一次的日志轮询——恰好在任务执行期间加压。
    exec daphne -b 0.0.0.0 -p 8001 ops_manager.asgi:application "$@"
    ;;

celery)
    # 默认队列：证书签发/分发。--concurrency=2 足够（acme.sh DNS 验证上限 900s，
    # 同主机分发已在任务内串行，调大只会抢同一批目标机）。
    exec celery -A ops_manager worker \
        -l "${CELERY_LOG_LEVEL:-info}" \
        --concurrency="${CELERY_CONCURRENCY:-2}" \
        "$@"
    ;;

celery-exec)
    # 任务执行专用队列。独立 worker 的理由：证书签发能占住 900s，
    # 与 ansible 挤同一队列时页面上是一条「执行中」且无报错的任务。
    #   nice 10   ansible 与 web 抢同一 4 个核时让 web 优先
    #   -n exec@%h 显式 node 名，否则与 celery 容器 node name 撞车，inspect/revoke 认错对象
    #   --concurrency=2  执行位不再全局唯一：失败等人处理/暂停确认/等 Jenkins 的任务
    #                    占位不干活，只有 1 个位置时会堵住全部后续任务。同主机同服务
    #                    由主机锁串行（apps/task_exec/host_lock.py）。理由与内存账见
    #                    deploy/opsmgr-celery-exec.conf
    exec nice -n 10 celery -A ops_manager worker \
        -l "${CELERY_LOG_LEVEL:-info}" \
        -Q task_exec \
        -n 'exec@%h' \
        --concurrency="${CELERY_EXEC_CONCURRENCY:-2}" \
        "$@"
    ;;

beat)
    # 只调度不干活，必须与 celery worker 成对部署，且只能起一个实例
    # （多个 beat 会把同一个任务重复投递）。
    # schedule 文件落在 /app/run（宿主具名目录），别落在只读或临时路径。
    exec celery -A ops_manager beat \
        -l "${CELERY_LOG_LEVEL:-info}" \
        --schedule=/app/run/celerybeat-schedule \
        "$@"
    ;;

feishu-ws)
    # 仅在「系统管理 → 飞书接入」设为长连接时启用。
    # 与回调模式同时开着会让同一个事件被处理两次（执行跑两遍）。
    exec python manage.py feishu_ws "$@"
    ;;

*)
    exec "$ROLE" "$@"
    ;;
esac
