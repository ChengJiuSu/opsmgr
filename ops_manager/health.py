"""存活 / 就绪探针。

容器编排要的是两个语义不同的端点，混成一个会在故障时做出错误决策：

  /healthz/  存活：只证明「这个进程还在应答 HTTP」。不碰数据库、不碰 Redis。
             它是 docker healthcheck 的判据 —— 数据库抖一下就重启 web 容器，
             只会把一次可恢复的抖动放大成一次雪崩。
  /readyz/   就绪：探数据库与 Redis，人工与上线脚本用。失败返回 503 并说明
             是哪一项挂了，不返回堆栈（这个端点无需认证）。

放在 ops_manager 包里（不参与 Cython 编译），与 settings/wsgi 同级：
探针属于部署设施，不属于业务模块。
"""
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def healthz(request):
    """存活探针：恒 200。"""
    return JsonResponse({'status': 'ok'})


@csrf_exempt
def readyz(request):
    """就绪探针：数据库 + Redis 都通才 200，否则 503。"""
    checks = {}
    ok = True

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        checks['database'] = 'ok'
    except Exception as exc:  # noqa: BLE001 — 探针要报出哪一项挂了，不吞异常类型
        checks['database'] = f'{type(exc).__name__}: {exc}'
        ok = False

    try:
        from django.core.cache import cache
        cache.set('healthz:probe', '1', 10)
        checks['cache'] = 'ok'
    except Exception as exc:  # noqa: BLE001
        checks['cache'] = f'{type(exc).__name__}: {exc}'
        ok = False

    return JsonResponse({'status': 'ok' if ok else 'unavailable', 'checks': checks},
                        status=200 if ok else 503)
