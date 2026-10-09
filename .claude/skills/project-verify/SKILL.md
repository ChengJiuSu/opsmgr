---
name: project-verify
description: devops-manager 改动完成后的统一验证与提交流程。当完成一轮功能开发/修复准备收尾，或用户要求"验证一下/测试一下/提交代码"时使用。
---

# 项目验证与提交流程（devops-manager）

按顺序执行，任何一步失败先修复再继续，如实报告结果。

## 1. 后端

```bash
cd /home/suchengjiu/workspace/devops-manager
./venv/bin/python manage.py makemigrations --check --dry-run   # 确认无遗漏迁移
./venv/bin/python manage.py check
./venv/bin/python manage.py test apps.<改动的app> ...          # 受影响 app 必跑
```

- 注意：系统 PATH 无 python，必须用 `./venv/bin/python`。
- task_exec 部分用例依赖本机 ansible（无则自动 skip，属正常）。
- 全量测试较慢（1 分钟+），改动面大或涉及公共组件（common/）时才跑全量。

## 2. 前端

```bash
cd frontend && npm run build
```

构建告警中 rolldown 的 PURE comment 警告是依赖自带的，可忽略。

## 3. 文档与记忆同步

- 后端接口/模型变化 → `docs/backend/modules/<module>.md`
- 页面/信息架构变化 → `docs/frontend/views/`
- 架构决策、用户确认的取舍 → 更新 auto-memory（cmdb-design / task-exec-design）

## 4. Git 提交

- 仅在用户要求时提交；提交信息中文、`type: 描述` 格式（feat/fix/chore/docs）。
- 提交前 `git status --short` 核对文件清单：运行产物（db.sqlite3、logs/、
  __pycache__、frontend/dist）已在 .gitignore，不应出现在暂存区。
- 功能代码与工程杂务（依赖、配置、gitignore）分开提交。

## 已知项目事实（避免重复踩坑）

- 数据迁移执行过后改文件不会重跑；同步已有库需用幂等脚本补种子，
  新种子只对全新环境生效。
- 字典项被外键 PROTECT 引用时删除会 400，先解除引用或停用。
- ExecutionTask 创建即调度执行（perform_create → dispatch），
  测试中需 `mock.patch('apps.task_exec.trigger.schedule')` 避免真实执行
  （发起逻辑已抽到 `apps/task_exec/trigger.py`，页面执行与接口触发共用）。
