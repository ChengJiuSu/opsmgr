#!/usr/bin/env python3
"""把后端业务代码用 Cython 编译成 .so，随后（可选）删除对应的 .py 源码。

用法（必须在仓库根目录执行，模块名由相对路径推导）：
    python deploy/cython/build.py            # 只编译，保留 .py（本地验证用）
    python deploy/cython/build.py --strip    # 编译后删除 .py / .c（镜像构建用）
    python deploy/cython/build.py --list     # 只打印将被编译的文件，不动手

⚠ 产出的 .so 带 ABI tag（cpython-314-x86_64-linux-gnu），
  **只能在与编译环境同 Python 主次版本、同 libc 的镜像里运行**。
  所以本脚本只在 deploy/docker/Dockerfile 的 builder 阶段跑，
  运行时镜像与 builder 用同一个 ubuntu:24.04 + deadsnakes python3.14 基线。

── 为什么这些目录不编 ────────────────────────────────────────
  migrations/   Django 迁移：无热点；makemigrations 需要源码；
                历史迁移是数据资产不是业务逻辑。
  __init__.py   包的 __init__ 编成扩展模块后 __path__ 语义变脆，
                而 Django 的 app registry、celery 的 autodiscover_tasks
                都依赖它能被当成普通包遍历。留纯 Python，代价是零。
  tests         不进生产镜像（见 .dockerignore）。

── 两个必设的编译指令（默认值都会在运行期才炸）──────────────
  annotation_typing=False
      Cython 默认把 `x: int` 这类注解当作 C 类型声明。Django/DRF 里的注解
      是给人和 drf-spectacular 看的，按 C 类型处理会让传 None/字符串的
      正常调用直接抛 TypeError。
  binding=True
      让编译出的函数保留 Python 函数的 __defaults__/签名内省能力。
      DRF 解析 ViewSet 方法、drf-spectacular 生成 schema 都要 inspect.signature，
      关掉会得到「参数全是 *args」的 schema，或直接报错。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# 编译范围：业务代码。入口与配置（manage.py / ops_manager/*）不编——
# 里面没有业务逻辑，且 settings/base.py 用 Path(__file__) 推导 BASE_DIR，
# 编译后一旦出问题排查成本远大于收益。
ROOTS = ('apps', 'common')

# 目录级排除（对路径中任意一段生效）
EXCLUDE_DIRS = {'migrations', '__pycache__', 'tests', 'fixtures', 'templates', 'static'}

# 文件级排除
EXCLUDE_NAMES = {'__init__.py', 'conftest.py', 'tests.py'}

# 单点排除（相对仓库根的 posix 路径）。编译后若某模块行为异常，
# 先把它加到这里换回纯 Python，再定位——别把整个 --strip 关掉。
# 首要嫌疑：channels 的 async 消费者（apps/*/consumers.py）。
EXCLUDE_PATHS: set[str] = set()

COMPILER_DIRECTIVES = {
    'language_level': '3',
    'annotation_typing': False,   # 见文件头说明，必须 False
    'binding': True,              # 见文件头说明，必须 True
    'embedsignature': True,       # 保留签名给 drf-spectacular
    'emit_code_comments': False,  # 别把 Python 源码当注释嵌进 .c（等于没编译）
    'always_allow_keywords': True,
}


def iter_sources() -> list[Path]:
    """收集待编译的 .py，返回相对仓库根的路径，已排序（便于比对构建日志）。"""
    found: list[Path] = []
    for root in ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for path in base.rglob('*.py'):
            parts = set(path.parts)
            if parts & EXCLUDE_DIRS:
                continue
            if path.name in EXCLUDE_NAMES:
                continue
            if path.as_posix() in EXCLUDE_PATHS:
                continue
            found.append(path)
    return sorted(found)


def strip_sources(sources: list[Path]) -> None:
    """删除已编译模块的 .py / .c，并清掉 __pycache__。

    必须做：.py 留着就等于没编译（源码照样可读），而且 importlib 在
    同目录同名时优先扩展模块，两份代码会静默漂移到不一致。
    """
    for path in sources:
        so_exists = any(path.parent.glob(f'{path.stem}.cpython-*.so'))
        if not so_exists:
            raise SystemExit(f'未找到 {path} 对应的 .so，中止（编译可能部分失败）')
        path.unlink()
        c_file = path.with_suffix('.c')
        if c_file.exists():
            c_file.unlink()
    for root in ROOTS:
        for cache in Path(root).rglob('__pycache__'):
            shutil.rmtree(cache, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strip', action='store_true',
                        help='编译后删除 .py / .c（镜像构建用）')
    parser.add_argument('--list', action='store_true',
                        help='只打印待编译文件清单')
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 1,
                        help='并行度，默认为 CPU 核数')
    args = parser.parse_args()

    if not Path('manage.py').exists():
        raise SystemExit('必须在仓库根目录执行：模块名由相对路径推导')

    sources = iter_sources()
    if not sources:
        raise SystemExit('没有找到待编译文件，检查 ROOTS 配置')

    if args.list:
        for path in sources:
            print(path)
        print(f'共 {len(sources)} 个模块', file=sys.stderr)
        return 0

    from Cython.Build import cythonize
    from setuptools import setup

    ext_modules = cythonize(
        [str(p) for p in sources],
        nthreads=args.jobs,
        compiler_directives=COMPILER_DIRECTIVES,
        quiet=True,
        # 源码变了才重新生成 .c；配合 docker layer 缓存能省掉大半构建时间
        force=False,
    )
    setup(
        name='ops-manager-compiled',
        ext_modules=ext_modules,
        script_args=['build_ext', '--inplace', f'--parallel={args.jobs}'],
    )

    print(f'已编译 {len(sources)} 个模块')
    if args.strip:
        strip_sources(sources)
        print('已删除对应 .py / .c')
    return 0


if __name__ == '__main__':
    sys.exit(main())
