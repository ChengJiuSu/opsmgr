<template>
  <!-- 任务列表 -->
  <el-card shadow="never">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input v-model="filter.keyword" placeholder="任务名称" clearable style="width:160px" @input="searchDebounced" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-select v-model="filter.task_type" placeholder="包含模块类型" clearable style="width:140px" @change="handleSearch">
          <el-option label="命令" value="command" />
          <el-option label="Playbook" value="playbook" />
          <el-option label="平台方法" value="method" />
        </el-select>
        <el-select v-model="filter.status" placeholder="状态" clearable style="width:110px" @change="handleSearch">
          <el-option label="等待中" value="pending" />
          <el-option label="执行中" value="running" />
          <el-option label="等待处理" value="paused" />
          <el-option label="成功" value="success" />
          <el-option label="失败" value="failed" />
        </el-select>
        <el-button :icon="Search" @click="handleSearch">查询</el-button>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
      </div>
      <el-button v-if="authStore.isStaff" type="primary" :icon="Plus" @click="openForm()">新建任务</el-button>
    </div>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="任务名称" min-width="130" show-overflow-tooltip />
      <el-table-column label="任务流" min-width="180">
        <template #default="{ row }">
          <el-tooltip :content="flowTooltip(row)" placement="top">
            <span class="flow-cell">
              <el-tag
                v-for="s in row.steps" :key="s.id"
                size="small" :type="typeTagType(s.task_type)" effect="plain" class="flow-tag"
              >{{ s.task_type_display }}</el-tag>
            </span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="项目组 / 渠道 / 环境" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">
          <span :class="{ unset: !row.project_group_name }">{{ row.project_group_name || '不限' }}</span> /
          <span :class="{ unset: !row.channel_name }">{{ row.channel_name || '不限' }}</span> /
          <span :class="{ unset: !row.environment_name, prod: row.environment_is_prod }">{{ row.environment_name || '不限' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="主机执行" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.exec_mode === 'serial' ? 'warning' : 'info'" effect="plain">
            {{ row.exec_mode === 'serial' ? '串行' : `并发×${row.forks || 10}` }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">
            <el-icon v-if="row.status === 'running'" class="is-loading"><Loading /></el-icon>
            {{ row.status_display }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="耗时" width="80">
        <template #default="{ row }">{{ row.duration != null ? row.duration + 's' : '-' }}</template>
      </el-table-column>
      <el-table-column prop="created_by_name" label="发起人" width="90" />
      <el-table-column prop="created_at" label="创建时间" width="160">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="330" fixed="right">
        <template #default="{ row }">
          <el-button v-if="authStore.isStaff" size="small" type="primary" :icon="CaretRight" :disabled="['running', 'paused'].includes(row.status)" @click="openExec(row)">执行</el-button>
          <el-button v-if="authStore.isStaff" size="small" :icon="EditPen" :disabled="['running', 'paused'].includes(row.status)" @click="openForm(row)">编辑</el-button>
          <el-button v-if="authStore.isStaff" size="small" :icon="CopyDocument" @click="handleCopy(row)">复制</el-button>
          <el-button size="small" :icon="View" @click="openLogs(row)">日志</el-button>
          <el-button v-if="authStore.isStaff" size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 16px; justify-content: flex-end"
      @change="fetchData"
    />
  </el-card>

  <!-- 执行日志面板（任务列表下方） -->
  <el-card v-if="logPanel.task" ref="logPanelRef" shadow="never" style="margin-top: 16px">
    <template #header>
      <div class="log-header">
        <span>
          执行日志 — {{ logPanel.task.name }}
          <el-tag size="small" :type="statusType(logPanel.task.status)" style="margin-left:8px">
            <el-icon v-if="logPanel.task.status === 'running'" class="is-loading"><Loading /></el-icon>
            {{ logPanel.task.status_display || logPanel.task.status }}
          </el-tag>
        </span>
        <el-button size="small" text :icon="Close" @click="closeLogs">关闭</el-button>
      </div>
    </template>
    <div ref="logBodyRef" class="log-body" :style="{ height: logBodyHeight + 'px' }">
      <div class="log-list">
        <el-table
          :data="logPanel.logs" size="small" highlight-current-row
          :max-height="logBodyHeight"
          :row-class-name="() => 'log-row'"
          @current-change="selectLog"
          ref="logTableRef"
        >
          <el-table-column label="执行时间" min-width="130">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="triggered_by_name" label="触发人" width="80" />
          <el-table-column label="模块" width="70">
            <template #default="{ row }">
              <span :class="{ danger: stepsFailed(row) }">{{ stepsSummary(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="主机" width="60">
            <template #default="{ row }">{{ row.server_count }}台</template>
          </el-table-column>
          <el-table-column label="方式" width="60">
            <template #default="{ row }">{{ row.exec_mode === 'serial' ? '串行' : '并发' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(row.status)">{{ row.status_display }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="70">
            <template #default="{ row }">{{ row.duration != null ? row.duration + 's' : '-' }}</template>
          </el-table-column>
        </el-table>
      </div>
      <div class="log-output">
        <div class="output-meta">
          <span v-if="logPanel.current">返回码：{{ logPanel.current.return_code ?? '-' }}</span>
          <span v-if="currentServers" class="muted">{{ currentServers }}</span>
          <el-tag v-if="logPanel.polling" type="info" size="small">实时刷新中…</el-tag>
          <span class="output-expand">
            <el-button size="small" :icon="FullScreen" @click="outputFullVisible = true">全屏查看</el-button>
            <el-button size="small" :icon="TopRight" @click="openLogPage">独立页打开</el-button>
          </span>
        </div>
        <!-- 暂停处理操作条：暂停确认模块（继续/中止）或模块失败等待（重试/跳过/中止） -->
        <el-alert v-if="logPanel.task.status === 'paused'" :type="pausedFailed ? 'error' : 'warning'" :closable="false" show-icon class="pause-bar">
          <template #title>
            <span class="pause-line">
              {{ pausedFailed ? '模块执行失败，任务流已暂停，等待人工处理' : '任务流已暂停，等待人工确认后继续' }}
              <span v-if="authStore.isStaff" class="pause-actions">
                <template v-if="pausedFailed">
                  <el-button size="small" type="primary" :loading="confirming" @click="handleConfirm('retry')">重试失败模块</el-button>
                  <el-button size="small" type="warning" :loading="confirming" @click="handleConfirm('skip')">跳过并继续</el-button>
                </template>
                <el-button v-else size="small" type="primary" :loading="confirming" @click="handleConfirm('continue')">继续执行</el-button>
                <el-button size="small" type="danger" :loading="confirming" @click="handleConfirm('abort')">中止任务流</el-button>
              </span>
            </span>
          </template>
        </el-alert>
        <StepOutput
          :output="logPanel.output"
          :steps-result="logPanel.current?.steps_result || []"
          :follow="logPanel.polling"
          class="output-host"
        />
      </div>
    </div>
  </el-card>

  <!-- 执行日志全屏浮层：与面板共用同一份输出数据，实时轮询同步生效 -->
  <el-dialog v-model="outputFullVisible" fullscreen :lock-scroll="true" class="output-full-dialog">
    <template #header>
      <div class="output-full-header">
        <span>
          执行日志 — {{ logPanel.task?.name }}
          <el-tag v-if="logPanel.task" size="small" :type="statusType(logPanel.task.status)" style="margin-left:8px">
            <el-icon v-if="logPanel.task.status === 'running'" class="is-loading"><Loading /></el-icon>
            {{ logPanel.task.status_display || logPanel.task.status }}
          </el-tag>
        </span>
        <span class="output-meta" style="margin-bottom:0">
          <span v-if="logPanel.current">返回码：{{ logPanel.current.return_code ?? '-' }}</span>
          <span v-if="currentServers" class="muted">{{ currentServers }}</span>
          <el-tag v-if="logPanel.polling" type="info" size="small">实时刷新中…</el-tag>
        </span>
      </div>
    </template>
    <el-alert v-if="logPanel.task?.status === 'paused'" :type="pausedFailed ? 'error' : 'warning'" :closable="false" show-icon class="pause-bar">
      <template #title>
        <span class="pause-line">
          {{ pausedFailed ? '模块执行失败，任务流已暂停，等待人工处理' : '任务流已暂停，等待人工确认后继续' }}
          <span v-if="authStore.isStaff" class="pause-actions">
            <template v-if="pausedFailed">
              <el-button size="small" type="primary" :loading="confirming" @click="handleConfirm('retry')">重试失败模块</el-button>
              <el-button size="small" type="warning" :loading="confirming" @click="handleConfirm('skip')">跳过并继续</el-button>
            </template>
            <el-button v-else size="small" type="primary" :loading="confirming" @click="handleConfirm('continue')">继续执行</el-button>
            <el-button size="small" type="danger" :loading="confirming" @click="handleConfirm('abort')">中止任务流</el-button>
          </span>
        </span>
      </template>
    </el-alert>
    <StepOutput
      :output="logPanel.output"
      :steps-result="logPanel.current?.steps_result || []"
      :follow="logPanel.polling"
      class="output-host output-host-full"
    />
  </el-dialog>

  <!-- 新建 / 编辑任务弹窗（任务流定义，仅保存不执行） -->
  <el-dialog v-model="formVisible" :title="form.id ? '编辑任务' : (copySourceName ? `复制任务（源：${copySourceName}）` : '新建任务')" width="760px" top="4vh">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="任务名称" prop="name">
            <el-input v-model="form.name" placeholder="如：版本发布流程" />
          </el-form-item>
        </el-col>
        <el-col :span="6">
          <el-form-item label-width="76px">
            <template #label>
              <el-tooltip content="模块内同时执行的主机数量（台）：默认 10 台一批；填 1 = 逐台串行执行；模块之间的并行用模块卡片上的「与相邻并行模块并发」" placement="top">
                <span>主机并发</span>
              </el-tooltip>
            </template>
            <el-input-number v-model="form.forks" :min="1" :max="200" controls-position="right" style="width:100%" />
            <div class="form-tip">同时执行的主机台数，1 = 逐台串行</div>
          </el-form-item>
        </el-col>
        <el-col :span="6">
          <el-form-item label="凭据" label-width="50px">
            <el-select v-model="form.credential" clearable placeholder="免密/密钥" style="width:100%">
              <el-option v-for="c in credentialOptions" :key="c.id" :label="c.name" :value="c.id" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>

      <!-- 全局主机范围（三维度必选，不再支持留空=不限） -->
      <el-form-item label="全局范围" required>
        <div class="dim-filter">
          <el-select v-model="form.project_group" placeholder="项目组（必选）" filterable @change="updateMatchedCount">
            <el-option v-for="d in dictOptions.projects" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="form.channel" placeholder="渠道（必选）" filterable @change="updateMatchedCount">
            <el-option v-for="d in dictOptions.channels" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="form.environment" placeholder="环境（必选）" filterable @change="updateMatchedCount">
            <el-option v-for="d in dictOptions.envs" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
        </div>
        <div class="form-tip">
          任务流的默认主机范围，项目组/渠道/环境<b>必选</b>（未选齐不予保存与执行），当前匹配
          <b :class="{ danger: matchedCount === 0 }">{{ matchedCount ?? '…' }}</b> 台可执行主机；
          模块可单独指定范围，具体主机在「执行」时确认
        </div>
      </el-form-item>

      <!-- 任务流模块列表 -->
      <el-divider content-position="left">任务流（{{ form.steps.length }} 个模块，按序执行，失败中止）</el-divider>
      <div v-for="(step, i) in form.steps" :key="i" class="step-card">
        <div class="step-head">
          <el-tag size="small" type="info" effect="dark" class="step-no">{{ i + 1 }}</el-tag>
          <el-input v-model="step.name" :placeholder="`模块名称，如：${stepNamePlaceholder(step)}`" style="width:200px" size="small" />
          <el-radio-group v-model="step.task_type" size="small">
            <el-radio-button value="command">命令</el-radio-button>
            <el-radio-button value="playbook">Playbook</el-radio-button>
            <el-radio-button value="method">平台方法</el-radio-button>
            <el-radio-button value="pause">暂停确认</el-radio-button>
          </el-radio-group>
          <span class="step-actions">
            <el-button size="small" text :icon="Top" :disabled="i === 0" @click="moveStep(i, -1)" />
            <el-button size="small" text :icon="Bottom" :disabled="i === form.steps.length - 1" @click="moveStep(i, 1)" />
            <el-button size="small" text type="danger" :icon="Delete" :disabled="form.steps.length === 1" @click="form.steps.splice(i, 1)" />
          </span>
        </div>

        <div class="step-body">
          <el-input
            v-if="step.task_type === 'command'"
            v-model="step.command" type="textarea" :rows="2" placeholder="Shell 命令，如：systemctl restart nginx"
          />
          <!-- playbook 模块直接选「方法管理」登记的 Ansible 剧本方法（provider 区分），剧本/tags 取自方法配置 -->
          <template v-if="step.task_type === 'playbook'">
            <el-select
              v-model="step.pbMethod" filterable style="width:100%"
              placeholder="选择方法管理登记的 Ansible 剧本方法"
              no-data-text="方法管理暂无 Ansible 剧本类方法，请先登记"
              @change="onPbMethodChange(step)"
            >
              <el-option v-for="m in playbookMethods" :key="m.id" :label="`${m.name}（${m.config?.playbook || '-'}）`" :value="m.id" />
            </el-select>
            <div v-if="pbMethodInfo(step)" class="form-tip">
              剧本：<code>{{ pbMethodInfo(step).config?.playbook }}</code>
              · 可选 tags：<code>{{ pbMethodInfo(step).config?.tags || '（未登记，留空执行全部）' }}</code>
            </div>
          </template>
          <template v-if="step.task_type === 'method'">
            <el-select
              v-model="step.method" filterable style="width:100%"
              placeholder="选择已启用的平台方法（非剧本类 provider）"
              no-data-text="方法管理暂无其他 provider 的平台方法"
            >
              <el-option v-for="m in otherMethods" :key="m.id" :label="`${m.name} (${m.provider_display})`" :value="m.id" />
            </el-select>
          </template>
          <!-- 暂停确认：无内容配置，执行到此暂停等待人工继续/中止 -->
          <el-alert
            v-if="step.task_type === 'pause'"
            type="warning" :closable="false" show-icon
          >
            任务流执行到本模块时暂停，等待管理员在日志面板确认「继续」或「中止」；
            超过下方超时时间未确认将自动中止任务流。
          </el-alert>

          <!-- playbook：tags 单选必填（候选取自所选剧本方法登记，不支持留空执行全部） -->
          <div v-if="step.task_type === 'playbook'" class="step-row">
            <el-select
              v-model="step.tag" filterable size="small" style="flex:1"
              placeholder="Tags（必选，单选一个）"
              no-data-text="所选剧本方法未登记 tags，请先在方法管理补全"
            >
              <el-option v-for="t in stepTagOptions(step)" :key="t" :label="t" :value="t" />
            </el-select>
            <el-input
              v-model="step.extra_vars" style="flex:2" size="small"
              placeholder="额外变量 --extra-vars（可留空）；CD 剧本自动注入最新制品 package_url/package_version/package_md5"
            />
          </div>
          <!-- 平台方法：JSON 参数框（jenkins 构建参数覆盖方法默认参数；其余 provider 自行消费） -->
          <el-input
            v-if="step.task_type === 'method'"
            v-model="step.paramsText" type="textarea" :rows="10" class="params-input"
            placeholder='方法参数（JSON 对象）。Jenkins 方法必须用 job_name 指定要触发的 job，其余键为构建参数（覆盖方法默认参数），如：
{
  "job_name": "build-app",
  "BRANCH": "master",
  "CHANNEL": "official"
}
其他 provider（云主机管理等）由对应执行器消费，如：
{
  "region": "cn-shanghai",
  "instance_ids": ["i-xxx", "i-yyy"]
}'
          />

          <div class="step-row">
            <span class="step-label">{{ step.task_type === 'pause' ? '确认超时' : '超时' }}</span>
            <el-input-number v-model="step.timeout" :min="10" :max="3600" size="small" style="width:130px" />
            <!-- 暂停确认模块不涉及目标主机，也不允许并行 -->
            <template v-if="step.task_type !== 'pause'">
            <el-tooltip content="相邻的、勾选并行的模块合并为一个并行组同时执行，组内全部成功才进入下一模块；不勾选=按序串行" placement="top">
              <el-checkbox v-model="step.run_parallel" size="small">与相邻并行模块并发</el-checkbox>
            </el-tooltip>
            <span class="step-label">主机范围</span>
            <el-radio-group v-model="step.use_custom_scope" size="small">
              <el-radio-button :value="false">跟随全局</el-radio-button>
              <el-radio-button :value="true">自定义</el-radio-button>
            </el-radio-group>
            </template>
            <template v-if="step.task_type !== 'pause' && step.use_custom_scope">
              <el-select v-model="step.project_group" placeholder="项目组" clearable size="small" style="width:120px" @change="updateStepCount(step)">
                <el-option v-for="d in dictOptions.projects" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
              <el-select v-model="step.channel" placeholder="渠道" clearable size="small" style="width:110px" @change="updateStepCount(step)">
                <el-option v-for="d in dictOptions.channels" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
              <el-select v-model="step.environment" placeholder="环境" clearable size="small" style="width:110px" @change="updateStepCount(step)">
                <el-option v-for="d in dictOptions.envs" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
              <span class="form-tip" style="margin:0">匹配 <b :class="{ danger: step._matched === 0 }">{{ step._matched ?? '…' }}</b> 台</span>
            </template>
          </div>
        </div>
      </div>
      <el-button size="small" :icon="Plus" style="width:100%" @click="addStep">添加模块</el-button>
    </el-form>
    <template #footer>
      <el-button @click="formVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSave">保存</el-button>
    </template>
  </el-dialog>

  <!-- 执行任务弹窗：任务流预览 + 全局主机勾选 -->
  <el-dialog v-model="execVisible" :title="`执行任务 — ${execTask?.name || ''}`" width="760px" top="4vh">
    <el-descriptions :column="3" border size="small" style="margin-bottom: 12px">
      <el-descriptions-item label="项目组">{{ execTask?.project_group_name || '不限' }}</el-descriptions-item>
      <el-descriptions-item label="渠道">{{ execTask?.channel_name || '不限' }}</el-descriptions-item>
      <el-descriptions-item label="环境">
        <span :class="{ prod: execTask?.environment_is_prod }">{{ execTask?.environment_name || '不限' }}</span>
      </el-descriptions-item>
    </el-descriptions>

    <div class="exec-hosts-header"><span>任务流（按序执行，失败中止后续模块）</span></div>
    <el-table :data="execTask?.steps || []" size="small" style="margin-bottom: 14px">
      <el-table-column label="#" width="42">
        <template #default="{ $index }">{{ $index + 1 }}</template>
      </el-table-column>
      <el-table-column prop="name" label="模块" min-width="110" show-overflow-tooltip />
      <el-table-column label="类型" width="120">
        <template #default="{ row }">
          <el-tag size="small" :type="typeTagType(row.task_type)" effect="plain">{{ row.task_type_display }}</el-tag>
          <el-tag v-if="row.run_parallel" size="small" type="danger" effect="plain" style="margin-left:4px">并行</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="内容" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">{{ stepContent(row) }}</template>
      </el-table-column>
      <el-table-column label="执行主机" width="150">
        <template #default="{ row }">
          <span :class="{ danger: stepHostCount(row) === 0 }">{{ stepHostLabel(row) }}</span>
        </template>
      </el-table-column>
    </el-table>

    <template v-if="hasGlobalStep">
      <div class="exec-hosts-header">
        <span>全局范围主机（默认全选，取消勾选即排除；仅作用于「跟随全局」的模块）</span>
        <span class="muted">已选 {{ execSelection.length }} / {{ execGlobal.length }} 台</span>
      </div>
      <el-table
        ref="hostTableRef"
        v-loading="execLoading"
        :data="execGlobal"
        size="small" stripe max-height="260"
        @selection-change="(rows) => (execSelection = rows)"
      >
        <el-table-column type="selection" width="42" />
        <el-table-column prop="name" label="主机名" min-width="120" show-overflow-tooltip />
        <!-- 连接地址口径：公网优先，NAT 主机回退私网，与实际 ansible 连接一致 -->
        <el-table-column label="IP" width="130">
          <template #default="{ row }">{{ row.public_ip || row.private_ip }}</template>
        </el-table-column>
        <el-table-column label="环境" width="100">
          <template #default="{ row }">
            <span :class="{ prod: row.environment_is_prod, unset: !row.environment_name }">{{ row.environment_name || '未分配' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="已规划服务" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ serviceCodes(row) }}</template>
        </el-table-column>
      </el-table>
      <el-alert v-if="!execLoading && !execGlobal.length" type="warning" :closable="false" show-icon style="margin-top:8px">
        全局范围未匹配到可执行主机（仅已在「服务器分配」中规划服务的主机可执行）
      </el-alert>
    </template>

    <template #footer>
      <el-button @click="execVisible = false">取消</el-button>
      <el-button type="primary" :disabled="!canExec" @click="confirmVisible = true">
        执行（{{ execTask?.steps?.length || 0 }} 个模块）
      </el-button>
    </template>
  </el-dialog>

  <!-- 执行确认框：任务信息 + 逐模块流程 -->
  <el-dialog v-model="confirmVisible" title="确认执行" width="600px" append-to-body>
    <el-descriptions :column="1" border size="small">
      <el-descriptions-item label="任务名称">{{ execTask?.name }}</el-descriptions-item>
      <el-descriptions-item label="项目组">{{ execTask?.project_group_name || '不限' }}</el-descriptions-item>
      <el-descriptions-item label="渠道">{{ execTask?.channel_name || '不限' }}</el-descriptions-item>
      <el-descriptions-item label="环境">
        <span :class="{ prod: hasProdTarget }">{{ execTask?.environment_name || '不限' }}</span>
      </el-descriptions-item>
    </el-descriptions>
    <div class="flow-title">任务流程（模块按序执行，失败中止后续）</div>
    <ol class="flow-steps">
      <li v-for="s in execTask?.steps || []" :key="s.id">
        <b>{{ s.name }}</b>（{{ s.task_type_display }}{{ s.run_parallel ? ' · 并行' : '' }}）：{{ stepContent(s) }} → {{ stepHostLabel(s) }}
      </li>
      <li>{{ execTask?.exec_mode === 'serial' ? '各模块内主机串行逐台执行（--forks 1）' : `各模块内主机并发执行（每批 ${execTask?.forks || 10} 台）` }}</li>
      <li>执行结果落一条执行日志（含逐模块结果），可在任务列表下方查看</li>
    </ol>
    <el-alert v-if="hasProdTarget" type="error" :closable="false" show-icon style="margin-top:10px">
      目标包含生产环境主机，请再次确认操作影响！
    </el-alert>
    <el-alert v-else type="warning" :closable="false" show-icon style="margin-top:10px">
      任务将通过 Ansible 在目标主机上执行，请谨慎操作。
    </el-alert>
    <template #footer>
      <el-button @click="confirmVisible = false">取消</el-button>
      <el-button :type="hasProdTarget ? 'danger' : 'primary'" :loading="executing" @click="doExecute">确认执行</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { debounce } from '../../utils/debounce'
import { useRouter } from 'vue-router'
import {
  Plus, Delete, Search, Refresh, View, Loading, CaretRight, EditPen, CopyDocument, Close, Top, Bottom, FullScreen, TopRight,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import StepOutput from '../../components/StepOutput.vue'
import {
  getServers, fetchDictOptions, projectGroupApi, channelApi, environmentApi,
} from '../../api/assets'
import {
  getTasks, createTask, updateTask, deleteTask, getMatchedServers, executeTask,
  getTaskOutput, getTaskLogs, getTaskLogDetail, getCredentials, getMethods, confirmTask,
} from '../../api/tasks'

const authStore = useAuthStore()
const router = useRouter()
const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', task_type: '', status: '' })

const credentialOptions = ref([])
const methodOptions = ref([])
const dictOptions = reactive({ projects: [], channels: [], envs: [] })

const statusType = (s) => ({ pending: 'info', running: 'warning', paused: 'primary', success: 'success', failed: 'danger' }[s] || 'info')
const typeTagType = (t) => ({ command: 'info', playbook: 'success', method: 'warning', pause: 'danger' }[t] || 'info')
const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')

const flowTooltip = (row) => (row.steps || []).map((s, i) => `${i + 1}. ${s.name}`).join('　')

function stepContent(s) {
  if (s.task_type === 'pause') return `等待人工确认（超时 ${s.timeout || 300}s）`
  if (s.task_type === 'method') return `${s.method_name || '-'}${s.tags ? ' · ' + s.tags : ''}`
  if (s.task_type === 'playbook') return `${s.playbook || '-'}${s.tags ? ' · ' + s.tags : ''}`
  return (s.command || '').slice(0, 50) || '-'
}

function serviceCodes(s) {
  return (s.services || [])
    .filter((x) => x.status === 'planned' || x.status === 'deployed')
    .map((x) => x.service_type_name)
    .join(' / ') || '-'
}

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.task_type) params.task_type = filter.task_type
    if (filter.status) params.status = filter.status
    const res = await getTasks(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
    // 日志面板打开时同步任务最新状态
    if (logPanel.task) {
      const fresh = tableData.value.find((t) => t.id === logPanel.task.id)
      if (fresh) logPanel.task = fresh
    }
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  searchDebounced.cancel()
  page.value = 1
  fetchData()
}

// 边输边查（300ms 防抖），无需回车
const searchDebounced = debounce(handleSearch)
onUnmounted(() => searchDebounced.cancel())

async function loadDictOptions() {
  if (dictOptions.projects.length) return
  const [proj, chan, env] = await Promise.allSettled([
    fetchDictOptions(projectGroupApi), fetchDictOptions(channelApi), fetchDictOptions(environmentApi),
  ])
  if (proj.status === 'fulfilled') dictOptions.projects = proj.value.data?.results || []
  if (chan.status === 'fulfilled') dictOptions.channels = chan.value.data?.results || []
  if (env.status === 'fulfilled') dictOptions.envs = env.value.data?.results || []
}

// 按维度取匹配主机数（page_size=1 仅拿 total）
async function fetchMatchedCount({ project_group, channel, environment }) {
  const params = { executable: 'true', page_size: 1 }
  if (project_group) params.project_group = project_group
  if (channel) params.channel = channel
  if (environment) params.env = environment
  const res = await getServers(params)
  return res.data?.total ?? 0
}

// ── 新建 / 编辑（任务流定义，仅保存）────────────────────
const formVisible = ref(false)
const formRef = ref()
const matchedCount = ref(null)
const copySourceName = ref('') // 复制来源任务名（仅弹窗标题提示，未保存前不落库）
const emptyStep = () => ({
  name: '', task_type: 'command', command: '', playbook: '', pbMethod: null, method: null,
  tag: '', extra_vars: '', paramsText: '', timeout: 300, run_parallel: false,
  use_custom_scope: false, project_group: null, channel: null, environment: null,
  _matched: null,
})
const emptyForm = () => ({
  id: null, name: '',
  project_group: null, channel: null, environment: null,
  forks: 10, credential: null,
  steps: [emptyStep()],
})
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入任务名称' }],
}

const methodInfo = (step) => methodOptions.value.find((m) => m.id === step.method) || null
const stepNamePlaceholder = (step) =>
  ({ command: '重启服务', playbook: '发布配置', method: '部署游戏服', pause: '上线前确认' }[step.task_type])

const splitTags = (s) => (s || '').split(',').map((t) => t.trim()).filter(Boolean)

// provider 区分：playbook 模块选 Ansible 剧本类方法，平台方法模块选其余 provider
const playbookMethods = computed(() => methodOptions.value.filter((m) => m.provider === 'ansible_playbook'))
const otherMethods = computed(() => methodOptions.value.filter((m) => m.provider !== 'ansible_playbook'))
const pbMethodInfo = (step) => playbookMethods.value.find((m) => m.id === step.pbMethod) || null

// 选定剧本方法后，剧本路径直接取方法配置；tags 清空待选
function onPbMethodChange(step) {
  step.playbook = pbMethodInfo(step)?.config?.playbook || ''
  step.tag = ''
}

// playbook 模块 tags 候选来自「方法管理」所选剧本方法的登记；已选值合并进候选避免编辑回显丢失
function stepTagOptions(step) {
  const tokens = new Set(splitTags(pbMethodInfo(step)?.config?.tags))
  if (step.tag) tokens.add(step.tag)
  return [...tokens]
}

async function updateMatchedCount() {
  matchedCount.value = null
  try {
    matchedCount.value = await fetchMatchedCount(form)
  } catch {
    matchedCount.value = null
  }
}

async function updateStepCount(step) {
  step._matched = null
  try {
    step._matched = await fetchMatchedCount(step)
  } catch {
    step._matched = null
  }
}

function addStep() {
  form.steps.push(emptyStep())
}

function moveStep(i, delta) {
  const j = i + delta
  ;[form.steps[i], form.steps[j]] = [form.steps[j], form.steps[i]]
}

async function openForm(row) {
  // row.id 为空表示「复制」预填（尚未落库），保留调用方设置的 copySourceName；
  // 真正编辑（row.id 存在）或全新新建（无 row）都清空复制提示
  if (!row || row.id) copySourceName.value = ''
  Object.assign(form, emptyForm(), { steps: [emptyStep()] })
  if (row) {
    Object.assign(form, {
      id: row.id, name: row.name,
      project_group: row.project_group || null, channel: row.channel || null,
      environment: row.environment || null,
      forks: row.exec_mode === 'serial' ? 1 : (row.forks || 10), credential: row.credential || null,
      steps: (row.steps || []).map((s) => ({
        name: s.name, task_type: s.task_type, command: s.command || '',
        playbook: s.playbook || '', pbMethod: null, method: s.method || null,
        tag: splitTags(s.tags)[0] || '', extra_vars: s.extra_vars || '',
        paramsText: s.params && Object.keys(s.params).length ? JSON.stringify(s.params, null, 2) : '',
        timeout: s.timeout || 300,
        run_parallel: s.run_parallel || false,
        use_custom_scope: s.use_custom_scope || false,
        project_group: s.project_group || null, channel: s.channel || null,
        environment: s.environment || null, _matched: null,
      })),
    })
    form.steps.filter((s) => s.use_custom_scope).forEach(updateStepCount)
  }
  formVisible.value = true
  formRef.value?.clearValidate()
  loadDictOptions()
  updateMatchedCount()
  const [cred, methods] = await Promise.allSettled([
    getCredentials({ page_size: 200 }),
    getMethods({ page_size: 200, enabled: 'true' }),
  ])
  if (cred.status === 'fulfilled') credentialOptions.value = cred.value.data?.results || []
  if (methods.status === 'fulfilled') methodOptions.value = methods.value.data?.results || []
  // 编辑回显：按剧本路径把 playbook 模块关联回方法管理的剧本方法
  form.steps
    .filter((s) => s.task_type === 'playbook' && !s.pbMethod && s.playbook)
    .forEach((s) => {
      const m = playbookMethods.value.find((x) => x.config?.playbook === s.playbook)
      if (m) s.pbMethod = m.id
    })
}

function validateSteps() {
  const typeNames = { command: '命令', playbook: 'Playbook', method: '平台方法' }
  for (const [i, s] of form.steps.entries()) {
    if (!s.name.trim()) s.name = `${typeNames[s.task_type]}模块${i + 1}` // 空名自动补
    const label = `模块 ${i + 1}「${s.name}」`
    if (s.task_type === 'pause') continue // 暂停确认无内容与主机范围
    if (s.task_type === 'command' && !s.command.trim()) return `${label}：请填写 Shell 命令`
    if (s.task_type === 'playbook') {
      if (!s.playbook) return `${label}：请选择方法管理登记的剧本方法`
      if (!s.tag) return `${label}：必须选择一个 tags（不支持留空执行全部）`
    }
    if (s.task_type === 'method') {
      if (!s.method) return `${label}：请选择平台方法`
      if (s.paramsText.trim()) {
        try {
          const parsed = JSON.parse(s.paramsText)
          if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
            return `${label}：方法参数必须为 JSON 对象（如 {"key": "value"}）`
          }
        } catch {
          return `${label}：方法参数不是合法 JSON，请检查格式`
        }
      }
    }
    if (s.use_custom_scope && (!s.project_group || !s.channel || !s.environment)) {
      return `${label}：自定义主机范围必须选齐项目组/渠道/环境`
    }
  }
  return null
}

function buildPayload() {
  return {
    name: form.name,
    project_group: form.project_group, channel: form.channel, environment: form.environment,
    exec_mode: (form.forks || 10) === 1 ? 'serial' : 'parallel',
    forks: form.forks || 10, credential: form.credential,
    steps: form.steps.map((s, i) => ({
      order: i + 1, name: s.name, task_type: s.task_type,
      command: s.task_type === 'command' ? s.command : '',
      playbook: s.task_type === 'playbook' ? s.playbook : '',
      method: s.task_type === 'method' ? s.method : null,
      tags: s.task_type === 'playbook' ? s.tag : '',
      extra_vars: s.task_type === 'playbook' ? s.extra_vars : '',
      params: s.task_type === 'method' && s.paramsText.trim() ? JSON.parse(s.paramsText) : {},
      timeout: s.timeout,
      run_parallel: s.task_type !== 'pause' && !!s.run_parallel,
      use_custom_scope: s.task_type !== 'pause' && s.use_custom_scope,
      project_group: s.task_type !== 'pause' && s.use_custom_scope ? s.project_group : null,
      channel: s.task_type !== 'pause' && s.use_custom_scope ? s.channel : null,
      environment: s.task_type !== 'pause' && s.use_custom_scope ? s.environment : null,
    })),
  }
}

async function handleSave() {
  await formRef.value.validate()
  if (!form.project_group || !form.channel || !form.environment) {
    ElMessage.error('全局主机范围的项目组/渠道/环境必选，请选齐后再保存')
    return
  }
  const stepError = validateSteps()
  if (stepError) {
    ElMessage.error(stepError)
    return
  }
  submitting.value = true
  try {
    if (form.id) {
      await updateTask(form.id, buildPayload())
      ElMessage.success('任务已保存')
    } else {
      await createTask(buildPayload())
      ElMessage.success('任务已保存，可在列表中点击「执行」发起')
    }
    formVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

// 复制任务流：仅本地预填表单（不调用后端，未点「保存」不落库），方便改到其他项目/渠道/环境后再保存
function handleCopy(row) {
  copySourceName.value = row.name
  openForm({ ...row, id: null, name: `${row.name} 副本` })
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除任务「${row.name}」及其全部执行日志吗？`, '警告', { type: 'warning' })
  await deleteTask(row.id)
  ElMessage.success('删除成功')
  if (logPanel.task?.id === row.id) closeLogs()
  fetchData()
}

// ── 执行（任务流预览 + 全局主机勾选 + 确认框）────────────
const execVisible = ref(false)
const confirmVisible = ref(false)
const execLoading = ref(false)
const executing = ref(false)
const execTask = ref(null)
const execGlobal = ref([])
const execStepsInfo = ref([]) // matched_servers 返回的模块主机来源（含自定义匹配结果）
const execSelection = ref([])
const hostTableRef = ref()

const hasGlobalStep = computed(() => (execTask.value?.steps || []).some((s) => s.task_type !== 'pause' && !s.use_custom_scope))
const stepInfoByOrder = computed(() => Object.fromEntries(execStepsInfo.value.map((s) => [s.order, s])))

const stepHostCount = (step) => {
  if (step.use_custom_scope) return stepInfoByOrder.value[step.order]?.total ?? 0
  return execSelection.value.length
}
const stepHostLabel = (step) => {
  if (step.task_type === 'pause') return '人工确认'
  return step.use_custom_scope ? `自定义 · ${stepHostCount(step)} 台` : `全局 · 已选 ${stepHostCount(step)} 台`
}

// 暂停确认模块不涉及主机，不参与「每模块必须有主机」校验
const canExec = computed(() =>
  (execTask.value?.steps || []).length > 0 &&
  (execTask.value?.steps || []).every((s) => s.task_type === 'pause' || stepHostCount(s) > 0),
)

const hasProdTarget = computed(() => {
  if (execTask.value?.environment_is_prod) return true
  if (execSelection.value.some((s) => s.environment_is_prod)) return true
  return execStepsInfo.value.some((st) => st.custom && (st.servers || []).some((s) => s.environment_is_prod))
})

async function openExec(row) {
  execTask.value = row
  execGlobal.value = []
  execStepsInfo.value = []
  execSelection.value = []
  execVisible.value = true
  execLoading.value = true
  try {
    const res = await getMatchedServers(row.id)
    execGlobal.value = res.data?.global?.results || []
    execStepsInfo.value = res.data?.steps || []
    // 全局范围默认全选，取消勾选即排除
    await nextTick()
    hostTableRef.value?.toggleAllSelection()
  } finally {
    execLoading.value = false
  }
}

async function doExecute() {
  executing.value = true
  try {
    const res = await executeTask(execTask.value.id, {
      server_ids: execSelection.value.map((s) => s.id),
    })
    ElMessage.success(res.msg || '已发起执行')
    confirmVisible.value = false
    execVisible.value = false
    fetchData()
    // 直接跳转执行日志：定位面板并轮询至完成
    openLogs({ ...execTask.value, status: 'running' }, { live: true })
  } finally {
    executing.value = false
  }
}

// ── 执行日志面板 ─────────────────────────────────────────
const logPanel = reactive({ task: null, logs: [], current: null, output: '', polling: false })
const logPanelRef = ref()
const logTableRef = ref()
const outputFullVisible = ref(false)
let pollTimer = null
let listTimer = null

// 面板工作区高度：按「面板滚动到屏幕顶部时正好铺满一屏」一次性计算的固定值（与当前滚动位置无关，
// 滚动过程中不重算，避免页面高度随滚动变化产生漂移感），仅窗口尺寸变化时更新，小屏保底 360px。
// 左侧记录表限 max-height 内部滚动，右侧输出框 flex 填满剩余高度，两栏恒等高，不受记录条数影响
const logBodyRef = ref()
const logBodyHeight = ref(420)
let heightRaf = null

function updateOutputHeight() {
  if (heightRaf) return
  heightRaf = requestAnimationFrame(() => {
    heightRaf = null
    const card = logPanelRef.value?.$el
    const body = logBodyRef.value
    if (!card || !body || !logPanel.task) return
    // 工作区相对卡片顶部的偏移（卡片头+内边距）是常量；滚动容器顶部位置也是常量 → 结果与滚动无关
    const containerTop = card.closest('.main-content')?.getBoundingClientRect().top ?? 0
    const delta = body.getBoundingClientRect().top - card.getBoundingClientRect().top
    // 40 = 卡片内边距 + 页面底部留白
    logBodyHeight.value = Math.max(360, Math.round(window.innerHeight - containerTop - delta - 40))
  })
}

const stepsSummary = (log) => {
  const steps = log.steps_result || []
  if (!steps.length) return '-'
  const ok = steps.filter((s) => s.status === 'success').length
  return `${ok}/${steps.length}`
}
const stepsFailed = (log) => (log.steps_result || []).some((s) => s.status === 'failed')

const currentServers = computed(() => {
  const snap = logPanel.current?.servers_snapshot
  if (!snap?.length) return ''
  const names = snap.slice(0, 5).map((s) => s.name).join('、')
  return `全局主机：${names}${snap.length > 5 ? ` 等 ${snap.length} 台` : ''}`
})

async function openLogs(row, { live = false } = {}) {
  logPanel.task = row
  logPanel.current = null
  logPanel.output = ''
  await refreshLogs()
  await nextTick()
  updateOutputHeight()
  logPanelRef.value?.$el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  if (live || ['running', 'pending', 'paused'].includes(row.status)) startPolling()
}

function closeLogs() {
  logPanel.task = null
  outputFullVisible.value = false
  stopPolling()
}

// 暂停处理：确认模块（继续/中止）或失败等待（重试/跳过/中止），中止与跳过需二次确认
const confirming = ref(false)
const pausedFailed = computed(() => logPanel.task?.pause_type === 'failed')
const CONFIRM_DONE_MSG = {
  continue: '已确认，任务流继续执行',
  abort: '已发送中止指令',
  retry: '已发送重试指令，失败模块将重新执行',
  skip: '已发送跳过指令，任务流继续执行后续模块',
}
async function handleConfirm(action) {
  if (action === 'abort') {
    await ElMessageBox.confirm('确定中止任务流吗？后续模块将不再执行。', '警告', { type: 'warning' })
  } else if (action === 'skip') {
    await ElMessageBox.confirm('确定跳过失败模块吗？任务流将继续执行后续模块（本次执行仍记为失败）。', '提示', { type: 'warning' })
  }
  confirming.value = true
  try {
    await confirmTask(logPanel.task.id, action)
    ElMessage.success(CONFIRM_DONE_MSG[action])
    // 状态由轮询自动刷新，无需手动改
  } finally {
    confirming.value = false
  }
}

// 新标签页打开独立日志页（URL 可分享，定位到当前选中的执行记录）
function openLogPage() {
  const { href } = router.resolve({
    name: 'TaskLog',
    params: { taskId: logPanel.task.id, logId: logPanel.current?.id },
  })
  window.open(href, '_blank')
}

async function refreshLogs() {
  if (!logPanel.task) return
  const res = await getTaskLogs(logPanel.task.id)
  logPanel.logs = res.data || []
  if (logPanel.logs.length) {
    await nextTick()
    logTableRef.value?.setCurrentRow(logPanel.logs[0])
  } else {
    logPanel.output = '（该任务暂无执行记录，点击列表「执行」发起）'
  }
}

async function selectLog(log) {
  if (!log || !logPanel.task) return
  logPanel.current = log
  const isLatest = logPanel.logs[0]?.id === log.id
  const running = ['pending', 'running', 'paused'].includes(logPanel.task.status)
  if (isLatest && running) {
    // 最新一次且执行中：输出走轻量轮询实时刷新
    logPanel.output = logPanel.task.output || '（执行中，等待输出…）'
    return
  }
  const res = await getTaskLogDetail(logPanel.task.id, log.id)
  logPanel.current = res.data
  logPanel.output = res.data?.output || '（暂无输出）'
}

// 增量轮询状态：outOffset = 服务端已回传长度；hasRaw = 当前展示的是真实日志（非占位）
let outOffset = 0
let hasRaw = false

function startPolling() {
  stopPolling()
  if (!logPanel.task) return
  logPanel.polling = true
  outOffset = 0
  hasRaw = false
  const taskId = logPanel.task.id
  pollTimer = setInterval(async () => {
    try {
      const res = await getTaskOutput(taskId, hasRaw ? outOffset : 0)
      if (logPanel.task?.id !== taskId) return stopPolling()
      const data = res.data || {}
      // output/offset/delta 属于日志流，不能并进任务对象（否则增量片段会污染 task.output）
      const { output: _o, offset: _f, delta: _d, ...taskFields } = data
      logPanel.task = { ...logPanel.task, ...taskFields }
      if (data.delta) {
        if (data.output) logPanel.output += data.output   // 只回传新增部分，追加
      } else {
        logPanel.output = data.output || '（执行中，等待输出…）'
      }
      outOffset = data.offset || 0
      hasRaw = hasRaw || !!data.output
      if (data.status === 'success' || data.status === 'failed') {
        stopPolling()
        fetchData()
        refreshLogs() // 执行完成：刷新日志列表并选中最新记录
      }
    } catch {
      stopPolling()
    }
  }, 1000)
}

function stopPolling() {
  logPanel.polling = false
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

onMounted(() => {
  fetchData()
  listTimer = setInterval(() => {
    if (tableData.value.some((t) => ['running', 'pending', 'paused'].includes(t.status))) {
      fetchData()
    }
  }, 5000)
  window.addEventListener('resize', updateOutputHeight)
})

onUnmounted(() => {
  stopPolling()
  if (listTimer) clearInterval(listTimer)
  window.removeEventListener('resize', updateOutputHeight)
  if (heightRaf) cancelAnimationFrame(heightRaf)
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; flex-wrap: wrap; }
.muted { color: #909399; }
.unset { color: #c0c4cc; }
.prod { color: #f56c6c; font-weight: 600; }
.danger { color: #f56c6c; }
.form-tip { color: #909399; font-size: 12px; line-height: 1.5; margin-top: 4px; }
.dim-filter { display: flex; gap: 8px; width: 100%; }
.dim-filter .el-select { flex: 1; }
code { background: #f5f7fa; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
.flow-cell { display: inline-flex; gap: 4px; flex-wrap: wrap; }
.flow-tag { margin: 0; }

/* 任务流模块卡片 */
.step-card {
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
  background: #fafafa;
}
.step-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.step-no { flex-shrink: 0; }
.step-actions { margin-left: auto; display: flex; }
.step-body { display: flex; flex-direction: column; gap: 8px; }
.step-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.step-label { font-size: 12px; color: #606266; flex-shrink: 0; }
.params-input :deep(textarea) { font-family: 'Courier New', monospace; font-size: 12px; }

.exec-hosts-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 8px; font-size: 13px; color: #606266;
}
.flow-title { font-weight: 600; margin: 14px 0 6px; font-size: 14px; }
.flow-steps { margin: 0; padding-left: 20px; color: #606266; font-size: 13px; line-height: 1.9; }
.log-header { display: flex; justify-content: space-between; align-items: center; }
.log-body { display: flex; gap: 16px; }
/* 执行记录列表占 1/3，详细日志占 2/3 */
.log-list { width: 33.33%; min-width: 300px; flex-shrink: 0; }
.log-list :deep(.log-row) { cursor: pointer; }
.log-output { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.output-meta { display: flex; gap: 16px; align-items: center; margin-bottom: 8px; color: #606266; font-size: 13px; }
.output-expand { margin-left: auto; }
/* 输出区高度：填满面板剩余高度（面板整体定高、内部滚动），全屏浮层占满剩余高度 */
.output-host { flex: 1; min-height: 0; }
.output-host-full { height: calc(100vh - 90px); border-radius: 0; }
.output-full-header {
  display: flex; justify-content: space-between; align-items: center;
  padding-right: 24px; font-size: 15px; font-weight: 600;
}
.output-full-dialog :deep(.el-dialog__body) { padding: 0 16px 16px; }
.pause-bar { margin-bottom: 8px; }
.pause-line { display: flex; align-items: center; gap: 12px; }
.pause-actions { display: inline-flex; gap: 8px; }
</style>
