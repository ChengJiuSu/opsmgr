# ============================================================================
# OpsManager 构建 Makefile（供 opsmanager.jenkinsfile 的步骤 3 调用）
#
# 用法：复制到 devops-manager 仓库根目录、改名为 Makefile 后提交。
#
# 共享库注入的变量：BUILD_TS、GIT_COMMIT
# opsmanager.jenkinsfile 通过 makeEnv 传入：SKIP_FRONTEND、DEPLOY_ENV、
#                                          DJANGO_SETTINGS_MODULE
#
# 约定：build 目标必须把可发布内容全部落到 output/，
#       共享库的步骤 4/5 以该目录为输入生成 md5 清单并打包。
# ============================================================================

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:

PYTHON                 ?= python3
VENV                   ?= venv
PIP                    := $(VENV)/bin/pip
PY                     := $(VENV)/bin/python
OUTPUT_DIR             ?= output
BUILD_TS               ?= dev
GIT_COMMIT             ?= unknown
SKIP_FRONTEND          ?= false
DEPLOY_ENV             ?= none
DJANGO_SETTINGS_MODULE ?= ops_manager.settings.prod
export DJANGO_SETTINGS_MODULE

.PHONY: all clean venv backend frontend static build

all: build

clean:
	rm -rf $(OUTPUT_DIR) staticfiles frontend/dist
	find . -name '__pycache__' -type d -prune -not -path './$(VENV)/*' -exec rm -rf {} +

venv:
	[ -d $(VENV) ] || $(PYTHON) -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt gunicorn

backend: venv
	# 只做静态检查性质的自检，不连数据库
	$(PY) manage.py check --deploy || true

static: venv
	$(PY) manage.py collectstatic --noinput

frontend:
	if [ "$(SKIP_FRONTEND)" = "true" ]; then
		echo "SKIP_FRONTEND=true，跳过前端构建"
		mkdir -p frontend/dist
	else
		cd frontend && npm ci && npm run build
	fi

# 产物目录里放「部署机上直接解包即可用」的内容，不含 venv 与 node_modules
build: backend static frontend
	rm -rf $(OUTPUT_DIR)
	mkdir -p $(OUTPUT_DIR)
	tar -cf - \
	    --exclude='./$(VENV)' \
	    --exclude='./frontend/node_modules' \
	    --exclude='./output' \
	    --exclude='./logs' \
	    --exclude='./media' \
	    --exclude='./.git' \
	    --exclude='*.sqlite3' \
	    --exclude='*.sqlite3.backup-*' \
	    --exclude='__pycache__' \
	    ./apps ./common ./ops_manager ./deploy ./ansible ./docs \
	    ./manage.py ./requirements.txt ./staticfiles ./frontend/dist \
	  | tar -xf - -C $(OUTPUT_DIR)
	# 版本信息随包走，部署后可核对
	printf 'build_ts=%s\ncommit=%s\ndeploy_env=%s\n' \
	    '$(BUILD_TS)' '$(GIT_COMMIT)' '$(DEPLOY_ENV)' > $(OUTPUT_DIR)/BUILD_INFO
	echo "产物已就绪: $(OUTPUT_DIR)"

# 测试
test:
	rm -rf $(OUTPUT_DIR)
	mkdir -p $(OUTPUT_DIR)
	tar -cf - \
	    --exclude='./$(VENV)' \
	    --exclude='./frontend/node_modules' \
	    --exclude='./output' \
	    --exclude='./logs' \
	    --exclude='./media' \
	    --exclude='./.git' \
	    --exclude='*.sqlite3' \
	    --exclude='*.sqlite3.backup-*' \
	    --exclude='__pycache__' \
	    ./apps ./common ./ops_manager ./deploy ./ansible ./docs \
	    ./manage.py ./requirements.txt ./staticfiles ./frontend/dist \
	  | tar -xf - -C $(OUTPUT_DIR)
	# 版本信息随包走，部署后可核对
	printf 'build_ts=%s\ncommit=%s\ndeploy_env=%s\n' \
	    '$(BUILD_TS)' '$(GIT_COMMIT)' '$(DEPLOY_ENV)' > $(OUTPUT_DIR)/BUILD_INFO
	echo "产物已就绪: $(OUTPUT_DIR)"