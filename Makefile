# ============================================================================
# OpsManager 构建 Makefile（供 opsmanager.jenkinsfile 的步骤 3 调用）
#
# 共享库注入的变量：BUILD_TS、GIT_COMMIT
# opsmanager.jenkinsfile 通过 makeEnv 传入：SKIP_FRONTEND、DEPLOY_ENV、
#                                          DJANGO_SETTINGS_MODULE
#
# 约定：build 目标必须把可发布内容全部落到 output/，
#       共享库的步骤 4/5 以该目录为输入生成 md5 清单并打包。
#
# ── 依赖策略（重要）────────────────────────────────────────────────────────
# 后端依赖【在 CI 侧预编成 wheelhouse 随产物走】，目标机离线安装：
#   构建期：docker + Dockerfile.builder（ubuntu:24.04 + py3.14，与生产同基线）
#           pip wheel -r requirements.lock  ->  output/wheelhouse/*.whl
#   部署期：deploy/install.sh 在目标机建 venv，
#           pip install --no-index --find-links=wheelhouse -r requirements.lock
# 目标机因此不需要 gcc、不需要访问 PyPI、每次装出的版本逐一相同。
# 唯一的运行时系统依赖是 libmysqlclient21（mysqlclient 链接它，一次性 bootstrap）。
# ============================================================================

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:

OUTPUT_DIR             ?= output
WHEELHOUSE             ?= wheelhouse
BUILD_TS               ?= dev
GIT_COMMIT             ?= unknown
SKIP_FRONTEND          ?= false
DEPLOY_ENV             ?= none
DJANGO_SETTINGS_MODULE ?= ops_manager.settings.prod
export DJANGO_SETTINGS_MODULE

# ── 构建基线（必须与生产机一致，改这里要同步目标机的运行时库）──────────────
UBUNTU_VERSION ?= 24.04
PYTHON_VERSION ?= 3.14
NODE_IMAGE     ?= node:22-bookworm-slim
BUILDER_IMAGE  ?= ops-manager-builder:$(UBUNTU_VERSION)-py$(PYTHON_VERSION)

# USE_DOCKER=false：在已装好 python$(PYTHON_VERSION)/node 的机器上直接本地跑
# （本地开发用；CI 上保持 true，才能保证基线一致）
USE_DOCKER ?= true
DOCKER     ?= docker

# 容器内以调用者的 uid/gid 运行，避免产物在 Jenkins workspace 里变成 root 所有；
# HOME 显式给 /tmp，否则 pip 在无家目录的 uid 下会报警告
DOCKER_RUN = $(DOCKER) run --rm \
	-u $$(id -u):$$(id -g) -e HOME=/tmp \
	-e DJANGO_SETTINGS_MODULE=$(DJANGO_SETTINGS_MODULE) \
	-v "$(CURDIR)":/src -w /src

# 本地回退路径用的解释器 / venv（USE_DOCKER=false 时才用到）
LOCAL_VENV ?= venv
LOCAL_PY   := $(LOCAL_VENV)/bin/python
LOCAL_PIP  := $(LOCAL_VENV)/bin/pip

# 容器内构建用的 venv，落在 workspace 相对路径下（容器与宿主同一挂载点）
CI_VENV ?= .ci-venv

.PHONY: all clean builder lock wheels ci-venv local-venv check static frontend build test lint

all: build

# ── 构建镜像 ──────────────────────────────────────────────────────────────
builder:
	if [ "$(USE_DOCKER)" != "true" ]; then echo "USE_DOCKER=false，跳过构建镜像"; exit 0; fi
	$(DOCKER) build \
	    -f deploy/docker/Dockerfile.builder \
	    --build-arg UBUNTU_VERSION=$(UBUNTU_VERSION) \
	    --build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
	    -t $(BUILDER_IMAGE) .

# ── 依赖锁定：改完 requirements.txt 执行本目标，并把 requirements.lock 提交 ──
# 不带 --generate-hashes，原因见 requirements.lock 头部注释
lock: builder
	if [ "$(USE_DOCKER)" = "true" ]; then
	    $(DOCKER_RUN) $(BUILDER_IMAGE) \
	        pip-compile --strip-extras --no-emit-index-url --no-header \
	                    --output-file requirements.lock requirements.txt
	else
	    pip-compile --strip-extras --no-emit-index-url --no-header \
	                --output-file requirements.lock requirements.txt
	fi
	echo "requirements.lock 已更新，记得一并提交"

# ── wheelhouse：产物里的离线依赖仓库 ──────────────────────────────────────
wheels: builder
	rm -rf $(WHEELHOUSE)
	mkdir -p $(WHEELHOUSE)
	if [ "$(USE_DOCKER)" = "true" ]; then
	    $(DOCKER_RUN) $(BUILDER_IMAGE) \
	        pip wheel --wheel-dir=$(WHEELHOUSE) -r requirements.lock
	else
	    pip wheel --wheel-dir=$(WHEELHOUSE) -r requirements.lock
	fi
	# 目标机是 --no-index 装的，缺一个包就整个部署失败，构建期先点清
	want=$$(grep -cE '^[a-z0-9]' requirements.lock)
	got=$$(ls -1 $(WHEELHOUSE)/*.whl | wc -l)
	echo "requirements.lock 声明 $$want 个包，wheelhouse 产出 $$got 个轮子"
	[ "$$got" -ge "$$want" ] || { echo "wheelhouse 不完整，中止" >&2; exit 1; }

# CI 侧 venv：装在 workspace 里（$(CI_VENV)），一次建好供 check/static/test 复用。
# 它的 shebang 指向容器内路径，只能在容器里用，所以不进产物（build 的 tar 不含它）。
# 这一步同时是 wheelhouse 的真实性验收：能离线装通，目标机才会装通。
ci-venv: wheels
	if [ "$(USE_DOCKER)" = "true" ]; then
	    rm -rf $(CI_VENV)
	    $(DOCKER_RUN) $(BUILDER_IMAGE) bash -c '
	        set -eu -o pipefail
	        python -m venv $(CI_VENV)
	        $(CI_VENV)/bin/pip install -q --no-index \
	            --find-links=$(WHEELHOUSE) -r requirements.lock
	        $(CI_VENV)/bin/python -c "import MySQLdb, django, ansible; print(\"依赖自检通过\")"'
	else
	    # 这里内联而非递归调 local-venv：recipe 文本里只要出现对 MAKE 变量的引用
	    # （连 shell 注释里的都算，make 会先展开变量再判定），整段 recipe 就会被
	    # 标记为递归调用，make -n 会照常执行它而不是只打印
	    [ -d $(LOCAL_VENV) ] || python$(PYTHON_VERSION) -m venv $(LOCAL_VENV)
	    $(LOCAL_PIP) install -U pip
	    $(LOCAL_PIP) install -r requirements.lock
	    $(LOCAL_PY) -c 'import MySQLdb, django, ansible; print("依赖自检通过")'
	fi

# 在构建基线里跑后端命令（collectstatic / check / test），保证与生产同解释器。
# $(1) 为要执行的 manage.py 子命令及参数。
define run_backend
	if [ "$(USE_DOCKER)" = "true" ]; then
	    $(DOCKER_RUN) $(BUILDER_IMAGE) $(CI_VENV)/bin/python manage.py $(1)
	else
	    $(LOCAL_PY) manage.py $(1)
	fi
endef

# USE_DOCKER=false 时的本地环境（本地开发路径）
local-venv:
	[ -d $(LOCAL_VENV) ] || python$(PYTHON_VERSION) -m venv $(LOCAL_VENV)
	$(LOCAL_PIP) install -U pip
	$(LOCAL_PIP) install -r requirements.lock

# ── 后端自检：真失败才算失败（原先 `|| true` 等于没检查）────────────────────
check: ci-venv
	$(call run_backend,check --deploy)

static: ci-venv
	rm -rf staticfiles
	$(call run_backend,collectstatic --noinput)

# ── 前端：CI 编译，产物随包走，目标机不装 node ────────────────────────────
frontend:
	if [ "$(SKIP_FRONTEND)" = "true" ]; then
	    echo "SKIP_FRONTEND=true，跳过前端构建"
	    mkdir -p frontend/dist
	    exit 0
	fi
	if [ "$(USE_DOCKER)" = "true" ]; then
	    $(DOCKER) run --rm -u $$(id -u):$$(id -g) -e HOME=/tmp \
	        -v "$(CURDIR)/frontend":/app -w /app $(NODE_IMAGE) \
	        bash -c 'npm ci --no-audit --no-fund && npm run build'
	else
	    cd frontend && npm ci --no-audit --no-fund && npm run build
	fi

# ── 产物 ──────────────────────────────────────────────────────────────────
# 目标机解包后只需 bash deploy/install.sh，不联网、不编译、不跑 collectstatic
build: check static frontend
	rm -rf $(OUTPUT_DIR)
	mkdir -p $(OUTPUT_DIR)
	tar -cf - \
	    --exclude='./frontend/node_modules' \
	    --exclude='__pycache__' \
	    ./apps ./common ./ops_manager ./deploy ./ansible ./docs \
	    ./manage.py ./requirements.txt ./requirements.lock \
	    ./staticfiles ./frontend/dist ./$(WHEELHOUSE) \
	  | tar -xf - -C $(OUTPUT_DIR)
	# 版本信息随包走，部署后可核对；install.sh 也会校验 python 主次版本
	printf 'build_ts=%s\ncommit=%s\ndeploy_env=%s\nubuntu_baseline=%s\npython_baseline=%s\n' \
	    '$(BUILD_TS)' '$(GIT_COMMIT)' '$(DEPLOY_ENV)' \
	    '$(UBUNTU_VERSION)' '$(PYTHON_VERSION)' > $(OUTPUT_DIR)/BUILD_INFO
	echo "产物已就绪: $(OUTPUT_DIR)"
	du -sh $(OUTPUT_DIR) $(OUTPUT_DIR)/$(WHEELHOUSE) $(OUTPUT_DIR)/staticfiles

# ── 流水线连通性冒烟：只验证「make 落 output/ → 共享库步骤 4/5 打包」这条链路，
# 不装依赖、不编前端、不需要 docker。原来的 test 目标干的是这件事，改名避免与
# 真正跑单测的 test 混淆；jenkinsfile 里如果还写着 `make test` 请改成 `make smoke`。
.PHONY: smoke
smoke:
	rm -rf $(OUTPUT_DIR)
	mkdir -p $(OUTPUT_DIR)
	tar -cf - \
	    --exclude='__pycache__' \
	    ./apps ./common ./ops_manager ./deploy ./ansible ./docs \
	    ./manage.py ./requirements.txt ./requirements.lock \
	  | tar -xf - -C $(OUTPUT_DIR)
	printf 'build_ts=%s\ncommit=%s\ndeploy_env=%s\nsmoke=true\n' \
	    '$(BUILD_TS)' '$(GIT_COMMIT)' '$(DEPLOY_ENV)' > $(OUTPUT_DIR)/BUILD_INFO
	echo "冒烟产物已就绪（不含 wheelhouse/staticfiles/frontend，不可用于部署）: $(OUTPUT_DIR)"

# ── 容器镜像（Cython 编译 + 多阶段构建）────────────────────────────────────
# 编译与运行共用 deploy/docker/Dockerfile 的 base 阶段，Python/libc 基线由
# Dockerfile 自身保证一致（.so 带 ABI tag，跨基线放过去只在 import 时才炸）。
IMAGE_NAME ?= ops-manager-backend
# CI 会注入 GIT_COMMIT；本地直接 make image 时退回当前 HEAD 的短 hash，
# 别打出一堆 :unknown —— 回滚时靠的就是这个 tag
IMAGE_TAG  ?= $(if $(filter unknown,$(GIT_COMMIT)),$(shell git rev-parse --short HEAD 2>/dev/null || echo dev),$(GIT_COMMIT))
# 容器内 app 用户的 uid/gid，**必须等于目标机上 logs/ media/ 目录的属主**
# （deploy/docker/init-dirs.sh 用的也是这两个值），否则 bind mount 写不进去。
APP_UID ?= 1000
APP_GID ?= 1000

.PHONY: image image-save cython-check compose-config

image:
	$(DOCKER) build -f deploy/docker/Dockerfile \
	    --build-arg UBUNTU_VERSION=$(UBUNTU_VERSION) \
	    --build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
	    --build-arg APP_UID=$(APP_UID) \
	    --build-arg APP_GID=$(APP_GID) \
	    --build-arg BUILD_TS=$(BUILD_TS) \
	    --build-arg GIT_COMMIT=$(GIT_COMMIT) \
	    -t $(IMAGE_NAME):$(IMAGE_TAG) \
	    -t $(IMAGE_NAME):latest .
	echo "镜像已就绪：$(IMAGE_NAME):$(IMAGE_TAG)（同时打了 latest）"
	echo "部署机拉不到 registry 时用 make image-save 导出 tar"

# 离线交付：导出镜像 tar（目标机 docker load -i）
image-save: image
	mkdir -p $(OUTPUT_DIR)
	$(DOCKER) save $(IMAGE_NAME):$(IMAGE_TAG) | gzip > $(OUTPUT_DIR)/$(IMAGE_NAME)-$(IMAGE_TAG).tar.gz
	ls -lh $(OUTPUT_DIR)/$(IMAGE_NAME)-$(IMAGE_TAG).tar.gz

# 只验证 Cython 能编过（不删源码、不打镜像）。改了 build.py 的排除规则、
# 或想抬 Cython 版本上界时，先跑这个。
# ⚠ 编译产物必须当场清掉：容器与宿主共用同一个挂载点，留下的 .so 会被本机
#   开发用的解释器优先加载（importlib 里扩展模块排在源码之前），改了 .py 却
#   不生效，排查起来毫无线索。
cython-check: builder
	$(DOCKER_RUN) $(BUILDER_IMAGE) bash -c '
	    set -eu -o pipefail
	    python -m venv /tmp/cyv
	    /tmp/cyv/bin/pip install -q -r requirements.lock -r deploy/docker/requirements-build.txt
	    /tmp/cyv/bin/python deploy/cython/build.py --list | tail -1
	    /tmp/cyv/bin/python deploy/cython/build.py' \
	  && ret=0 || ret=$$?
	find apps common \( -name '*.so' -o -name '*.c' \) -delete
	rm -rf build
	[ "$$ret" = 0 ] && echo "编译通过（产物已清理）" || { echo "编译失败" >&2; exit "$$ret"; }

compose-config:
	$(DOCKER) compose config -q && echo "compose 配置校验通过"

# ── 测试 / 代码风格 ───────────────────────────────────────────────────────
# 用 dev settings（sqlite）跑，不依赖 CI 上有 MySQL
test: ci-venv
	$(call run_backend,test apps --settings=ops_manager.settings.dev -v 2)

lint:
	$(LOCAL_VENV)/bin/black --check apps common ops_manager
	$(LOCAL_VENV)/bin/isort --check-only apps common ops_manager

clean:
	rm -rf $(OUTPUT_DIR) $(WHEELHOUSE) $(CI_VENV) staticfiles frontend/dist
	find . -name '__pycache__' -type d -prune -not -path './$(LOCAL_VENV)/*' -exec rm -rf {} +
