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
      <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openForm()">新建任务</el-button>
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
          <el-button v-if="canRunTask" size="small" type="primary" :icon="CaretRight" :disabled="['running', 'paused'].includes(row.status)" @click="openExec(row)">执行</el-button>
          <el-button v-if="canWrite" size="small" :icon="EditPen" :disabled="['running', 'paused'].includes(row.status)" @click="openForm(row)">编辑</el-button>
          <el-button v-if="canWrite" size="small" :icon="CopyDocument" @click="handleCopy(row)">复制</el-button>
          <el-button size="small" :icon="View" @click="openLogs(row)">日志</el-button>
          <el-button v-if="canWrite" size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
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
          <el-tag v-if="following" type="info" size="small">实时刷新中…</el-tag>
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
              <span v-if="canRunTask" class="pause-actions">
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
          :follow="following"
          :log-id="logPanel.current?.id"
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
          <el-tag v-if="following" type="info" size="small">实时刷新中…</el-tag>
        </span>
      </div>
    </template>
    <el-alert v-if="logPanel.task?.status === 'paused'" :type="pausedFailed ? 'error' : 'warning'" :closable="false" show-icon class="pause-bar">
      <template #title>
        <span class="pause-line">
          {{ pausedFailed ? '模块执行失败，任务流已暂停，等待人工处理' : '任务流已暂停，等待人工确认后继续' }}
          <span v-if="canRunTask" class="pause-actions">
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
      :follow="following"
      :log-id="logPanel.current?.id"
      class="output-host output-host-full"
    />
  </el-dialog>

  <!-- 新建 / 编辑任务弹窗（任务流定义，仅保存不执行） -->
  <el-dialog
    v-model="formVisible"
    :title="form.id ? '编辑任务' : (copySourceName ? `复制任务（源：${copySourceName}）` : '新建任务')"
    width="1040px" top="6vh" class="exec-dialog"
  >
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
            <div class="form-tip">1 = 逐台串行</div>
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
          模块默认的主机范围，项目组/渠道/环境<b>必选</b>，当前匹配
          <b :class="{ danger: matchedCount === 0 }">{{ matchedCount ?? '…' }}</b> 台可执行主机；
          模块可单独覆盖范围与服务类型，具体主机在「执行」时确认
        </div>
        <!-- 关闭后执行表单不预勾选，必须人工挑主机（后端同样拒绝缺省 server_ids） -->
        <el-checkbox v-model="form.default_select_all" size="small">
          执行时默认全选范围主机
        </el-checkbox>
        <span class="form-tip" style="margin:0 0 0 8px">
          取消后发起执行时不预勾选，必须人工确认要跑哪些主机
        </span>
      </el-form-item>

      <!-- 任务流模块列表：流程图 + 左模块树 + 右单模块编辑（与执行弹窗一致） -->
      <div class="exec-section">
        <b>任务流</b>
        <span class="muted">{{ form.steps.length }} 个模块，按序执行，失败中止后续模块</span>
        <el-button size="small" text type="primary" style="margin-left:auto" @click="formFlowOpen = !formFlowOpen">
          {{ formFlowOpen ? '收起流程图' : '查看流程图' }}
        </el-button>
      </div>
      <FlowGraph
        v-if="formFlowOpen"
        :steps="formFlowSteps" :active="formView === ALL_STEPS ? null : formView"
        :meta-map="formFlowMeta" clickable
        style="margin-bottom:12px"
        @select="formView = $event"
      />

      <div class="exec-flow">
        <div class="flow-nav">
          <div class="nav-header">任务流</div>
          <div
            v-for="(step, i) in form.steps" :key="i"
            class="nav-node" :class="{ active: formView === i }" @click="formView = i"
          >
            <span class="nav-dot" :class="formNavDot(step)" />
            <span class="nav-order">{{ i + 1 }}</span>
            <span class="nav-title" :title="step.name">{{ step.name || `模块${i + 1}` }}</span>
            <span class="nav-count muted">{{ FORM_TYPE_LABEL[step.task_type] }}</span>
          </div>
          <div class="nav-node nav-root" :class="{ active: formView === ALL_STEPS }" @click="formView = ALL_STEPS">
            <span class="nav-dot root" />
            <span class="nav-title">全部模块流程</span>
          </div>
          <el-button size="small" :icon="Plus" style="width:100%;margin-top:6px" @click="addStep">添加模块</el-button>
        </div>

        <div class="form-steps">
      <div v-for="{ step, i } in visibleFormSteps" :key="i" class="step-card">
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
            <el-button size="small" text type="danger" :icon="Delete" :disabled="form.steps.length === 1" @click="removeStep(i)" />
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
              @change="onMethodChange(step)"
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
              placeholder="额外变量 --extra-vars（可留空，CD 剧本自动注入 package_* 制品变量）"
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

          <!-- 模块设置：执行（超时/并行）与主机（范围/服务类型）分两行 -->
          <div class="step-settings">
            <div class="setting-row">
              <span class="setting-label">{{ step.task_type === 'pause' ? '确认超时' : '超时' }}</span>
              <el-input-number v-model="step.timeout" :min="10" :max="3600" size="small" style="width:120px" />
              <!-- 暂停确认模块不涉及目标主机，也不允许并行 -->
              <el-tooltip
                v-if="step.task_type !== 'pause'" placement="top"
                content="相邻的、勾选并行的模块合并为一个并行组同时执行，组内全部成功才进入下一模块；不勾选=按序串行"
              >
                <el-checkbox v-model="step.run_parallel" size="small">与相邻并行模块并发</el-checkbox>
              </el-tooltip>
            </div>

            <div v-if="step.task_type !== 'pause'" class="setting-row">
              <span class="setting-label">主机范围</span>
              <el-radio-group v-model="step.use_custom_scope" size="small" @change="updateStepCount(step)">
                <el-radio-button :value="false">跟随全局</el-radio-button>
                <el-radio-button :value="true">自定义</el-radio-button>
              </el-radio-group>
              <template v-if="step.use_custom_scope">
                <el-select v-model="step.project_group" placeholder="项目组" clearable size="small" style="width:110px" @change="updateStepCount(step)">
                  <el-option v-for="d in dictOptions.projects" :key="d.id" :label="d.name" :value="d.id" />
                </el-select>
                <el-select v-model="step.channel" placeholder="渠道" clearable size="small" style="width:100px" @change="updateStepCount(step)">
                  <el-option v-for="d in dictOptions.channels" :key="d.id" :label="d.name" :value="d.id" />
                </el-select>
                <el-select v-model="step.environment" placeholder="环境" clearable size="small" style="width:100px" @change="updateStepCount(step)">
                  <el-option v-for="d in dictOptions.envs" :key="d.id" :label="d.name" :value="d.id" />
                </el-select>
              </template>
              <!-- 服务类型：与主机范围正交，跟随全局/自定义都可再按服务类型收窄 -->
              <el-tooltip placement="top" content="在上述主机范围内，只对已规划/已部署该服务类型的主机执行本模块（必选）">
                <span class="setting-label inline required" :class="{ active: step.service_type }">服务类型</span>
              </el-tooltip>
              <el-select
                v-model="step.service_type" placeholder="必选" filterable
                size="small" style="width:120px" @change="updateStepCount(step)"
              >
                <el-option v-for="d in dictOptions.serviceTypes" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
              <el-tag
                v-if="step.use_custom_scope || step.service_type" class="match-tag"
                size="small" effect="plain" :type="step._matched === 0 ? 'danger' : 'info'"
              >
                匹配 <b class="match-num">{{ step._matched ?? '…' }}</b> 台
              </el-tag>
            </div>
          </div>
        </div>
      </div>
        </div>
      </div>
    </el-form>
    <template #footer>
      <el-button @click="formVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSave">保存</el-button>
    </template>
  </el-dialog>

  <!-- 执行任务弹窗：任务流预览 + 全局主机勾选 -->
  <el-dialog v-model="execVisible" width="1040px" top="6vh" class="exec-dialog">
    <template #header>
      <span class="exec-title">执行任务</span>
      <span class="exec-title-name">{{ execTask?.name }}</span>
    </template>

    <!-- 任务概览：维度 + 模块数 -->
    <div class="exec-meta">
      <div class="exec-meta-cell"><span class="k">项目组</span><b>{{ execTask?.project_group_name || '不限' }}</b></div>
      <div class="exec-meta-cell"><span class="k">渠道</span><b>{{ execTask?.channel_name || '不限' }}</b></div>
      <div class="exec-meta-cell">
        <span class="k">环境</span>
        <b :class="{ prod: execTask?.environment_is_prod }">{{ execTask?.environment_name || '不限' }}</b>
      </div>
      <div class="exec-meta-cell">
        <span class="k">模块</span>
        <b>{{ execTask?.steps?.length || 0 }} 个</b>
        <span class="muted">· {{ execTask?.exec_mode === 'serial' ? '主机串行' : '按序执行' }}</span>
      </div>
    </div>

    <div class="exec-section">
      <b>任务流</b>
      <span class="muted">按序执行，失败中止后续模块 · 每个模块单独勾选目标主机</span>
      <b v-if="!execTask?.default_select_all" class="danger">需人工勾选</b>
      <el-button size="small" text type="primary" style="margin-left:auto" @click="flowGraphOpen = !flowGraphOpen">
        {{ flowGraphOpen ? '收起流程图' : '查看流程图' }}
      </el-button>
    </div>

    <!-- 模块执行顺序示意图：点节点切到该模块 -->
    <FlowGraph
      v-if="flowGraphOpen"
      :steps="execTask?.steps || []" :active="execView === ALL_STEPS ? null : execView"
      :meta-map="flowMetaMap" clickable
      style="margin-bottom:12px"
      @select="selectExecView"
    />

    <div v-loading="execLoading" class="exec-flow">
      <!-- 左侧模块树：任务流（全部模块）/ 单个模块，与执行日志面板一致 -->
      <div class="flow-nav">
        <div class="nav-header">任务流</div>
        <div
          v-for="(s, i) in execTask?.steps || []" :key="s.id"
          class="nav-node" :class="{ active: execView === s.id }" @click="selectExecView(s.id)"
        >
          <span class="nav-dot" :class="navDotClass(s)" />
          <span class="nav-order">{{ i + 1 }}</span>
          <span class="nav-title" :title="s.name">{{ s.name }}</span>
          <span v-if="s.task_type === 'pause'" class="nav-count muted">人工</span>
          <span v-else class="nav-count" :class="{ danger: stepHostCount(s) === 0 }">{{ stepHostCount(s) }} 台</span>
        </div>
        <div class="nav-node nav-root" :class="{ active: execView === ALL_STEPS }" @click="selectExecView(ALL_STEPS)">
          <span class="nav-dot root" />
          <span class="nav-title">全部模块流程</span>
        </div>
      </div>

      <div class="exec-steps">
      <div v-for="(s, i) in visibleExecSteps" :key="s.id" class="exec-step">
        <div class="exec-step-index">{{ execView === ALL_STEPS ? i + 1 : stepOrderOf(s) }}</div>
        <div class="exec-step-main">
          <div class="exec-step-head">
            <b class="exec-step-name">{{ s.name }}</b>
            <!-- Role/命令/方法独立展示：竖线分隔，标签灰、值蓝色高亮 -->
            <template v-if="stepBadgeValue(s)">
              <span class="exec-step-sep" />
              <span class="badge">{{ stepBadge(s) }}</span>
              <span class="exec-step-role">{{ stepBadgeValue(s) }}</span>
            </template>
            <code v-if="stepPrimary(s)" class="exec-step-primary">{{ stepPrimary(s) }}</code>
            <el-tag v-if="s.run_parallel" size="small" type="danger" effect="plain">并行</el-tag>
            <!-- 模块勾选的服务类型：主机可能规划了多种服务，这里明确本模块只打哪一种 -->
            <template v-if="s.service_type_name">
              <span class="exec-step-sep" />
              <span class="badge">SERVICE_TYPE</span>
              <span class="exec-step-service">{{ s.service_type_name }}</span>
            </template>
            <span v-else-if="s.task_type !== 'pause'" class="exec-step-service-none">服务类型未设置（存量任务按不限执行）</span>
            <span v-if="s.task_type !== 'pause'" class="exec-step-count">
              <template v-if="s.use_custom_scope">
                自定义 <b class="num">{{ stepHostCount(s) }}</b> 台
              </template>
              <template v-else>
                已选 <b class="num" :class="{ danger: stepHostCount(s) === 0 }">{{ stepHostCount(s) }}</b>
                /{{ stepCandidates(s).length }} 台
              </template>
            </span>
          </div>

          <!-- 暂停确认：不涉及主机 -->
          <div v-if="s.task_type === 'pause'" class="form-tip">执行到本模块暂停，等待人工确认，不涉及目标主机</div>

          <!-- 自定义范围：按模块维度全量执行，不参与勾选，只列出影响的主机 -->
          <template v-else-if="s.use_custom_scope">
            <div class="form-tip">自定义范围按模块维度全量执行，不可勾选：</div>
            <div class="step-hosts">
              <el-tag v-for="name in stepHostNames(s)" :key="name" size="small" effect="plain" class="host-chip">
                {{ name }}
              </el-tag>
            </div>
          </template>

          <!-- 跟随全局：本模块独立勾选（候选已按模块服务类型收窄）-->
          <div v-else class="exec-step-hosts">
            <div class="host-filter">
              <el-input
                v-model="stepFilters[s.id].keyword" placeholder="搜索主机名/IP" clearable size="small"
                style="width:180px" @input="syncStepSelection(s)" @clear="syncStepSelection(s)"
              />
              <el-select
                v-model="stepFilters[s.id].serviceType" placeholder="服务类型（全部）" clearable size="small"
                style="width:150px" @change="syncStepSelection(s)"
              >
                <el-option v-for="t in stepServiceTypes(s)" :key="t" :label="t" :value="t" />
              </el-select>
              <span class="muted" style="font-size:12px">当前 {{ stepVisibleHosts(s).length }} 台</span>
              <el-button class="pick-btn" size="small" text type="primary" @click="selectStepHosts(s, true)">全选当前</el-button>
              <el-button size="small" text @click="selectStepHosts(s, false)">取消当前</el-button>
            </div>
            <!-- 主机表自成滚动区：表头黏顶，行少时不留空白；超出时底部渐隐 -->
            <div class="host-table-wrap" :class="{ 'has-more': stepVisibleHosts(s).length > VISIBLE_ROWS }">
            <el-table
              :ref="(el) => setStepTable(s.id, el)"
              :data="stepVisibleHosts(s)"
              size="small" :max-height="HOST_LIST_HEIGHT" row-key="id" scrollbar-always-on
              class="host-table" :row-class-name="({ row }) => stepRowClass(s, row)"
              @selection-change="(rows) => onStepSelectionChange(s, rows)"
            >
              <el-table-column type="selection" width="42" reserve-selection />
              <el-table-column label="主机名" min-width="240" show-overflow-tooltip>
                <template #default="{ row }">
                  <span class="host-prefix">{{ splitHostName(row.name).prefix }}</span
                  ><span class="host-suffix">{{ splitHostName(row.name).suffix }}</span>
                </template>
              </el-table-column>
              <!-- 连接地址口径：公网优先，NAT 主机回退私网，与实际 ansible 连接一致 -->
              <el-table-column label="IP" width="130">
                <template #default="{ row }">{{ row.public_ip || row.private_ip }}</template>
              </el-table-column>
              <el-table-column label="环境" width="72">
                <template #default="{ row }">
                  <span :class="{ prod: row.environment_is_prod, unset: !row.environment_name }">
                    {{ row.environment_name || '未分配' }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="已规划服务" min-width="170">
                <template #default="{ row }">
                  <el-tooltip :content="serviceCodes(row)" placement="top">
                    <span class="host-services">
                      <el-tag
                        v-for="x in hostServices(row)" :key="x.service_type"
                        size="small" :effect="x.service_type === s.service_type ? 'dark' : 'plain'"
                        :type="x.service_type === s.service_type ? 'primary' : 'info'"
                      >{{ x.service_type_name }}</el-tag>
                      <span v-if="!hostServices(row).length" class="unset">-</span>
                    </span>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column prop="project_group_name" label="项目组" min-width="100" show-overflow-tooltip />
            </el-table>
            </div>
            <div v-if="stepVisibleHosts(s).length > VISIBLE_ROWS" class="exec-step-foot">
              列表限高显示 {{ VISIBLE_ROWS }} 台，滚动查看其余 {{ stepVisibleHosts(s).length - VISIBLE_ROWS }} 台
            </div>
            <el-alert
              v-if="!execLoading && !stepCandidates(s).length" type="warning" :closable="false" show-icon
              style="margin-top:6px"
            >
              本模块范围内无可执行主机（需在「服务器分配」中规划服务{{ s.service_type_name ? `，且服务类型为「${s.service_type_name}」` : '' }}）
            </el-alert>
          </div>
        </div>
      </div>
      </div>
    </div>

    <!-- 汇总：累计=各模块相加（同一台被多个模块用会重复计），去重=实际涉及主机数 -->
    <div v-if="!execLoading" class="exec-summary">
      <span>汇总</span>
      <b>{{ execTask?.steps?.length || 0 }}</b> 个模块 ·
      累计目标主机 <b>{{ totalTargetHosts }}</b> 台 ·
      去重 <b>{{ uniqueTargetHosts }}</b> 台
      <span v-if="hasProdTarget" class="prod">· 含生产环境主机</span>
    </div>

    <template #footer>
      <div class="exec-footer">
        <span class="muted">
          共 {{ execTask?.steps?.length || 0 }} 个模块，累计目标主机 {{ totalTargetHosts }} 台
        </span>
        <span>
          <el-button @click="execVisible = false">取消</el-button>
          <el-button type="primary" :disabled="!canExec" @click="confirmVisible = true">
            执行（{{ execTask?.steps?.length || 0 }} 个模块）
          </el-button>
        </span>
      </div>
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
        <div v-if="stepHostCount(s)" class="flow-hosts">{{ stepHostsTooltip(s) }}</div>
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
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { debounce } from '../../utils/debounce'
import { useRoute, useRouter } from 'vue-router'
import {
  Plus, Delete, Search, Refresh, View, Loading, CaretRight, EditPen, CopyDocument, Close, Top, Bottom, FullScreen, TopRight,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import { openTaskLogStream } from '../../utils/taskLogStream'
import { createLogBuffer, applyFull, applyDelta, resetBuffer } from '../../utils/taskLogBuffer'
import StepOutput from '../../components/StepOutput.vue'
import FlowGraph from '../../components/FlowGraph.vue'
import {
  getServers, fetchDictOptions, projectGroupApi, channelApi, environmentApi, serviceTypeApi,
} from '../../api/assets'
import {
  getTasks, createTask, updateTask, deleteTask, getMatchedServers, executeTask,
  getTaskOutput, getTaskLogs, getTaskLogDetail, getCredentials, getMethods, confirmTask,
} from '../../api/tasks'

const authStore = useAuthStore()
// 改任务定义要 exec 写档；发起执行与处理暂停看的是能力位（can_execute），
// 两者是正交的：只能跑不能改，正是「普通运维」这一档
const canWrite = computed(() => authStore.can('exec', 'write'))
const canRunTask = computed(() => authStore.capability('can_execute'))
const route = useRoute()
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
const dictOptions = reactive({ projects: [], channels: [], envs: [], serviceTypes: [] })

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

// 主机已规划/已部署的服务列表；命中模块服务类型的排在最前（表格里高亮显示）
function hostServices(row) {
  return (row.services || []).filter((x) => x.status === 'planned' || x.status === 'deployed')
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
    // 日志面板打开时同步任务最新状态（合并而不是替换：面板整块由
    // v-if="logPanel.task" 控制，保持对象身份可免掉一次整块重渲染/重挂）
    if (logPanel.task) {
      const fresh = tableData.value.find((t) => t.id === logPanel.task.id)
      if (fresh) Object.assign(logPanel.task, fresh)
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
  const [proj, chan, env, svc] = await Promise.allSettled([
    fetchDictOptions(projectGroupApi), fetchDictOptions(channelApi), fetchDictOptions(environmentApi),
    fetchDictOptions(serviceTypeApi),
  ])
  if (proj.status === 'fulfilled') dictOptions.projects = proj.value.data?.results || []
  if (chan.status === 'fulfilled') dictOptions.channels = chan.value.data?.results || []
  if (env.status === 'fulfilled') dictOptions.envs = env.value.data?.results || []
  if (svc.status === 'fulfilled') dictOptions.serviceTypes = svc.value.data?.results || []
}

// 按维度取匹配主机数（page_size=1 仅拿 total）
async function fetchMatchedCount({ project_group, channel, environment, service_type }) {
  const params = { executable: 'true', page_size: 1 }
  if (project_group) params.project_group = project_group
  if (channel) params.channel = channel
  if (environment) params.env = environment
  // 服务类型与 executable 由后端合并进同一条 services 记录判定
  if (service_type) params.service_type = service_type
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
  service_type: null, _matched: null,
})
const emptyForm = () => ({
  id: null, name: '',
  project_group: null, channel: null, environment: null,
  forks: 10, credential: null, default_select_all: true,
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
  updateAllStepCounts()
}

// 模块匹配台数：自定义范围用模块自身维度，跟随全局用任务全局维度；
// 两者都叠加模块服务类型。既不自定义也没选服务类型时台数即全局台数，不单独请求
async function updateStepCount(step) {
  step._matched = null
  if (step.task_type === 'pause' || (!step.use_custom_scope && !step.service_type)) return
  const dims = step.use_custom_scope ? step : form
  try {
    step._matched = await fetchMatchedCount({ ...dims, service_type: step.service_type })
  } catch {
    step._matched = null
  }
}

// 全局维度变更后，跟随全局且带服务类型过滤的模块台数需一并刷新
function updateAllStepCounts() {
  form.steps.forEach((s) => {
    if (s.use_custom_scope || s.service_type) updateStepCount(s)
  })
}

// 选定平台方法后，模块未指定服务类型则跟随方法登记的服务类型（与部署前置校验同源）
function onMethodChange(step) {
  const m = methodInfo(step)
  if (m?.service_type && !step.service_type) {
    step.service_type = m.service_type
    updateStepCount(step)
  }
}

// 编辑弹窗的模块视图：索引 = 只编辑该模块，ALL_STEPS = 堆叠全部
const formView = ref(0)
const formFlowOpen = ref(true)
const FORM_TYPE_LABEL = { command: '命令', playbook: 'Playbook', method: '平台方法', pause: '暂停确认' }
const visibleFormSteps = computed(() => {
  const list = form.steps.map((step, i) => ({ step, i }))
  return formView.value === ALL_STEPS ? list : list.filter((x) => x.i === formView.value)
})
// 流程图数据源：表单模块没有主键，用下标当节点 id（与 formView 同一口径）
const formFlowSteps = computed(() => form.steps.map((step, i) => ({
  id: i, name: step.name || `模块${i + 1}`, task_type: step.task_type, run_parallel: step.run_parallel,
})))
const formFlowMeta = computed(() => Object.fromEntries(form.steps.map((step, i) => [
  i,
  step.task_type === 'pause' ? '等待人工确认'
    : (dictOptions.serviceTypes.find((d) => d.id === step.service_type)?.name || '未选服务类型'),
])))
const formNavDot = (step) => (step.task_type === 'pause' ? 'pause' : 'ready')

function addStep() {
  form.steps.push(emptyStep())
  formView.value = form.steps.length - 1
}

// 删除模块后把视图落到相邻模块，避免停在已不存在的下标上
function removeStep(i) {
  form.steps.splice(i, 1)
  if (formView.value !== ALL_STEPS) {
    formView.value = Math.min(formView.value, form.steps.length - 1)
  }
}

function moveStep(i, delta) {
  const j = i + delta
  ;[form.steps[i], form.steps[j]] = [form.steps[j], form.steps[i]]
  if (formView.value === i) formView.value = j
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
      default_select_all: row.default_select_all !== false,
      steps: (row.steps || []).map((s) => ({
        name: s.name, task_type: s.task_type, command: s.command || '',
        playbook: s.playbook || '', pbMethod: null, method: s.method || null,
        tag: splitTags(s.tags)[0] || '', extra_vars: s.extra_vars || '',
        paramsText: s.params && Object.keys(s.params).length ? JSON.stringify(s.params, null, 2) : '',
        timeout: s.timeout || 300,
        run_parallel: s.run_parallel || false,
        use_custom_scope: s.use_custom_scope || false,
        project_group: s.project_group || null, channel: s.channel || null,
        environment: s.environment || null, service_type: s.service_type || null,
        _matched: null,
      })),
    })
    form.steps.forEach(updateStepCount)
  }
  formView.value = 0
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
    if (!s.service_type) return `${label}：必须选择服务类型（不支持不限）`
  }
  return null
}

function buildPayload() {
  return {
    name: form.name,
    project_group: form.project_group, channel: form.channel, environment: form.environment,
    exec_mode: (form.forks || 10) === 1 ? 'serial' : 'parallel',
    forks: form.forks || 10, credential: form.credential,
    default_select_all: form.default_select_all,
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
      // 服务类型与自定义范围正交：跟随全局的模块也可只按服务类型收窄
      service_type: s.task_type !== 'pause' ? s.service_type : null,
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
const execGlobal = ref([])          // 全局范围匹配主机（各模块的候选来源）
const execStepsInfo = ref([])       // matched_servers 返回的模块主机来源
// 逐模块勾选：模块 id → 已勾选主机 id 集合（跟随全局的模块各自独立）
const stepSelection = ref({})
// 逐模块筛选条件：模块 id → { keyword, serviceType }
const stepFilters = ref({})
const stepTables = {}               // 模块 id → el-table 实例（重放勾选用）
const syncingSteps = new Set()      // 重放期间忽略该模块的表格事件

const stepInfoByOrder = computed(() => Object.fromEntries(execStepsInfo.value.map((s) => [s.order, s])))

// 模块候选主机：跟随全局取全局匹配集，带服务类型的按后端命中集收窄
const stepCandidates = (step) => {
  if (step.task_type === 'pause' || step.use_custom_scope) return []
  const allowed = stepInfoByOrder.value[step.order]?.server_ids
  if (step.service_type && allowed) {
    const hit = new Set(allowed)
    return execGlobal.value.filter((s) => hit.has(s.id))
  }
  return execGlobal.value
}

// 模块实际执行的主机：自定义范围取后端匹配结果，跟随全局取本模块勾选
const stepHosts = (step) => {
  if (step.task_type === 'pause') return []
  if (step.use_custom_scope) return stepInfoByOrder.value[step.order]?.servers || []
  const picked = stepSelection.value[step.id]
  return picked ? stepCandidates(step).filter((s) => picked.has(s.id)) : []
}
const stepHostCount = (step) => stepHosts(step).length
const HOST_CHIP_LIMIT = 4
const stepHostNames = (step) => stepHosts(step).map((s) => s.name)
const stepHostsTooltip = (step) => stepHostNames(step).join('、') || '无'
const stepHostLabel = (step) => {
  if (step.task_type === 'pause') return '人工确认'
  const svc = step.service_type_name ? ` · ${step.service_type_name}` : ''
  return step.use_custom_scope
    ? `自定义${svc} · ${stepHostCount(step)} 台`
    : `已选${svc} · ${stepHostCount(step)} / ${stepCandidates(step).length} 台`
}

// 模块筛选后的可见行：筛选只改可见范围，勾选按 id 保留（切筛选不丢）。
// 用 computed 缓存数组：模板里现算会每次渲染产生新数组，el-table 视为
// 数据变更并清空勾选（勾一台就全没了）
const stepVisibleMap = computed(() => {
  const out = {}
  ;(execTask.value?.steps || []).forEach((step) => {
    if (step.task_type === 'pause' || step.use_custom_scope) return
    const f = stepFilters.value[step.id] || { keyword: '', serviceType: '' }
    const kw = (f.keyword || '').trim().toLowerCase()
    out[step.id] = stepCandidates(step).filter((s) => {
      if (f.serviceType && !(s.services || []).some((x) => x.service_type_name === f.serviceType)) return false
      if (!kw) return true
      return `${s.name} ${s.public_ip || ''} ${s.private_ip || ''}`.toLowerCase().includes(kw)
    })
  })
  return out
})
const stepVisibleHosts = (step) => stepVisibleMap.value[step.id] || []

// 模块候选主机里出现过的服务类型（筛选下拉候选，无需额外请求）
const stepServiceTypes = (step) => {
  const names = new Set()
  stepCandidates(step).forEach((s) => (s.services || []).forEach((x) => names.add(x.service_type_name)))
  return [...names].sort()
}

function setStepTable(stepId, el) {
  if (el) stepTables[stepId] = el
  else delete stepTables[stepId]
}

// 表格勾选变化：只用可见行结果覆盖可见部分，被筛掉的已勾选主机原样保留
function onStepSelectionChange(step, rows) {
  if (syncingSteps.has(step.id)) return
  const visibleIds = new Set(stepVisibleHosts(step).map((s) => s.id))
  const next = new Set([...(stepSelection.value[step.id] || [])].filter((id) => !visibleIds.has(id)))
  rows.forEach((r) => next.add(r.id))
  stepSelection.value = { ...stepSelection.value, [step.id]: next }
}

function selectStepHosts(step, checked) {
  const next = new Set(stepSelection.value[step.id] || [])
  stepVisibleHosts(step).forEach((s) => (checked ? next.add(s.id) : next.delete(s.id)))
  stepSelection.value = { ...stepSelection.value, [step.id]: next }
  syncStepSelection(step)
}

// 把模块勾选集重放到表格复选框（数据/筛选变化后调用）
async function syncStepSelection(step) {
  syncingSteps.add(step.id)
  await nextTick()
  const table = stepTables[step.id]
  table?.clearSelection()
  const picked = stepSelection.value[step.id] || new Set()
  stepVisibleHosts(step).forEach((s) => {
    if (picked.has(s.id)) table?.toggleRowSelection(s, true)
  })
  await nextTick()
  syncingSteps.delete(step.id)
}

// 模块摘要（对齐执行弹窗的两行展示）：主体内容 + 类型徽标 + 徽标值
// 主机表滚动区：限高 228px = 表头 38 + 5 行 × 38，行高由 CSS 压到 38px；
// 可见行数由高度推算，底部提示与实际能看到的台数保持一致
const HOST_LIST_HEIGHT = 228
const HOST_ROW_HEIGHT = 38
const VISIBLE_ROWS = Math.floor(HOST_LIST_HEIGHT / HOST_ROW_HEIGHT) - 1
const stepPrimary = (step) => (step.task_type === 'playbook' ? step.playbook : '')
const stepBadge = (step) =>
  ({ playbook: 'ROLE', command: 'CMD', method: 'METHOD', pause: 'PAUSE' }[step.task_type] || '')
const stepBadgeValue = (step) => {
  if (step.task_type === 'playbook') return step.tags
  if (step.task_type === 'command') return step.command
  if (step.task_type === 'method') return step.method_name
  return `等待人工确认 ${step.timeout || 300}s`
}
// 主机名分段：末尾两段（nginx-01 / test-02）加深，前缀降灰，长名字便于扫读
const splitHostName = (name) => {
  const parts = (name || '').split('-')
  if (parts.length <= 2) return { prefix: '', suffix: name || '' }
  return { prefix: `${parts.slice(0, -2).join('-')}-`, suffix: parts.slice(-2).join('-') }
}
// 已勾选行加极浅蓝底，未选行留白，全选时也能看出层次
const stepRowClass = (step, row) => (stepSelection.value[step.id]?.has(row.id) ? 'row-picked' : '')

const totalTargetHosts = computed(() =>
  (execTask.value?.steps || []).reduce((n, s) => n + stepHostCount(s), 0))
const uniqueTargetHosts = computed(() => {
  const ids = new Set()
  ;(execTask.value?.steps || []).forEach((s) => stepHosts(s).forEach((h) => ids.add(h.id)))
  return ids.size
})

// 模块视图：全部模块（堆叠）或单个模块，切换后重放该模块勾选到表格
const ALL_STEPS = '__all__'
const execView = ref(ALL_STEPS)
const visibleExecSteps = computed(() => {
  const steps = execTask.value?.steps || []
  return execView.value === ALL_STEPS ? steps : steps.filter((s) => s.id === execView.value)
})
const stepOrderOf = (step) => (execTask.value?.steps || []).findIndex((s) => s.id === step.id) + 1
// 流程图：默认展开，节点副标题显示本模块目标台数
const flowGraphOpen = ref(true)
const flowMetaMap = computed(() => Object.fromEntries(
  (execTask.value?.steps || []).map((s) => [
    s.id,
    s.task_type === 'pause' ? '等待人工确认' : `${stepHostCount(s)} 台`,
  ]),
))
const navDotClass = (step) => {
  if (step.task_type === 'pause') return 'pause'
  return stepHostCount(step) ? 'ready' : 'empty'
}
async function selectExecView(view) {
  execView.value = view
  await nextTick()
  visibleExecSteps.value
    .filter((s) => s.task_type !== 'pause' && !s.use_custom_scope)
    .forEach(syncStepSelection)
}

const globalSteps = computed(() =>
  (execTask.value?.steps || []).filter((s) => s.task_type !== 'pause' && !s.use_custom_scope))

// 暂停确认模块不涉及主机，不参与「每模块必须有主机」校验
const canExec = computed(() =>
  (execTask.value?.steps || []).length > 0 &&
  (execTask.value?.steps || []).every((s) => s.task_type === 'pause' || stepHostCount(s) > 0),
)

const hasProdTarget = computed(() => {
  if (execTask.value?.environment_is_prod) return true
  return (execTask.value?.steps || []).some((s) => stepHosts(s).some((h) => h.environment_is_prod))
})

async function openExec(row) {
  execTask.value = row
  execGlobal.value = []
  execStepsInfo.value = []
  // 主机数据到位前模板已渲染，筛选/勾选容器先按模块建好，避免 v-model 到 undefined
  // 默认定位第一个模块（无模块时退回全部流程）
  execView.value = row.steps?.[0]?.id ?? ALL_STEPS
  stepSelection.value = Object.fromEntries((row.steps || []).map((s) => [s.id, new Set()]))
  stepFilters.value = Object.fromEntries(
    (row.steps || []).map((s) => [s.id, { keyword: '', serviceType: '' }]),
  )
  execVisible.value = true
  execLoading.value = true
  try {
    const res = await getMatchedServers(row.id)
    execGlobal.value = res.data?.global?.results || []
    execStepsInfo.value = res.data?.steps || []
    // 每个跟随全局的模块独立初始化：默认全选候选（可勾除）；
    // 任务关闭默认全选时留空，必须逐模块人工挑主机
    const selection = { ...stepSelection.value }
    globalSteps.value.forEach((step) => {
      selection[step.id] = new Set(
        row.default_select_all !== false ? stepCandidates(step).map((s) => s.id) : [],
      )
    })
    stepSelection.value = selection
    await nextTick()
    visibleExecSteps.value
      .filter((step) => step.task_type !== 'pause' && !step.use_custom_scope)
      .forEach(syncStepSelection)
  } finally {
    execLoading.value = false
  }
}

async function doExecute() {
  executing.value = true
  try {
    // step_servers 为准（逐模块），server_ids 传并集供未单独勾选的模块兜底
    const stepServers = {}
    const union = new Set()
    globalSteps.value.forEach((step) => {
      const ids = stepHosts(step).map((s) => s.id)
      stepServers[step.id] = ids
      ids.forEach((id) => union.add(id))
    })
    const res = await executeTask(execTask.value.id, {
      step_servers: stepServers,
      server_ids: [...union],
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
let stream = null   // WebSocket 日志流（不可用时为 null，走 pollTimer）
let selectSeq = 0   // 选中序号：异步取详情返回时用它丢弃已被覆盖的旧请求
let resyncTimer = null
let lastFrameAt = 0            // 上次收到日志帧的时刻（推送静默兜底用）
const RESYNC_QUIET_MS = 15000  // 静默这么久就按偏移补拉一次
// 正在看「执行中」那条记录：看历史记录时通道仍开着（状态/模块表照收，暂停操作条
// 不会因为翻了一眼历史就消失），但日志帧不再往展示缓冲里写
const liveTail = ref(false)
const following = computed(() => logPanel.polling && liveTail.value)
// 实时日志缓冲：按绝对偏移把窗口帧拼成越来越长的完整日志（见 utils/taskLogBuffer）
const buf = createLogBuffer()

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
  liveTail.value = false
  resetBuffer(buf)
  await refreshLogs()
  await nextTick()
  updateOutputHeight()
  logPanelRef.value?.$el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  // selectLog 选中「执行中」的最新记录时已经开跟随了，这里只兜住刚发起执行
  // （列表状态还没变成 running）的情况
  if (!logPanel.polling && (live || ['running', 'pending', 'paused'].includes(row.status))) {
    liveTail.value = true
    startFollow()
  }
}

function closeLogs() {
  logPanel.task = null
  outputFullVisible.value = false
  liveTail.value = false
  stopFollow()
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
  const seq = ++selectSeq
  const taskId = logPanel.task.id
  logPanel.current = log
  // 「是否执行中」看这条记录自己的状态，不看任务快照（快照可能是上一次执行留下的
  // 终态，据此判断会把执行中的记录当历史去读详情，页面上就是「暂无输出」）
  const isLatest = logPanel.logs[0]?.id === log.id
  const live = ['pending', 'running', 'paused'].includes(log.status || logPanel.task.status)
  dbg('selectLog', { id: log.id, isLatest, live, status: log.status })
  if (isLatest && live) {
    // 最新一次且执行中：输出走实时通道（WebSocket / 轮询回退），重连以取回完整快照
    logPanel.output = '（执行中，等待输出…）'
    liveTail.value = true
    startFollow()
    return
  }
  liveTail.value = false
  const res = await getTaskLogDetail(taskId, log.id)
  if (seq !== selectSeq || logPanel.task?.id !== taskId) return  // 期间已切走：丢弃
  logPanel.current = res.data
  logPanel.output = res.data?.output || '（暂无输出）'
}

// 排查用开关：控制台执行 localStorage.taskLogDebug = '1' 后刷新页面，
// 面板这边的关键节点也会打到控制台（与 StepOutput 的输出对照看）
const dbg = (...args) => {
  try {
    if (new URLSearchParams(location.search).get('logdebug') === '1'
        || localStorage.getItem('taskLogDebug') === '1') {
      console.log('[logPanel]', ...args)
    }
  } catch { /* 隐私模式读不到 localStorage，忽略 */ }
}

// ── 实时日志：WebSocket 优先，失败回退轮询 ────────────────
function render() {
  logPanel.output = buf.text || '（执行中，等待输出…）'
}

function applySteps(steps) {
  // 执行中的模块表（含后续 pending 模块）随推送更新，模块列表据此保持完整
  dbg('applySteps', (steps || []).map((x, i) => `${i + 1}:${x.status}`).join(','))
  if (logPanel.current) logPanel.current = { ...logPanel.current, steps_result: steps || [] }
}

function startFollow() {
  stopFollow()
  if (!logPanel.task) return
  logPanel.polling = true
  resetBuffer(buf)
  const taskId = logPanel.task.id
  lastFrameAt = Date.now()
  stream = openTaskLogStream(taskId, {
    onFull: (frame) => {
      if (logPanel.task?.id !== taskId) return stopFollow()
      lastFrameAt = Date.now()
      if (!liveTail.value) return
      applyFull(buf, frame)
      render()
    },
    onDelta: (frame) => {
      if (logPanel.task?.id !== taskId) return stopFollow()
      lastFrameAt = Date.now()
      if (!liveTail.value) return
      applyDelta(buf, frame)
      render()
    },
    onSteps: (steps) => {
      if (logPanel.task?.id !== taskId) return stopFollow()
      applySteps(steps)
    },
    onStatus: (data) => {
      if (logPanel.task?.id !== taskId) return stopFollow()
      logPanel.task = {
        ...logPanel.task,
        status: data.status,
        pause_type: data.pause_type,
        return_code: data.return_code,
        duration: data.duration,
      }
      if (data.status === 'success' || data.status === 'failed') finish()
    },
    onFallback: () => {
      stream = null
      if (logPanel.polling) startPolling()  // 通道不可用：退回轮询接口
    },
  })
  startResyncTimer()
}

// 推送静默兜底：连接活着（pong 正常）但一直收不到日志帧时，按绝对偏移补拉一次。
// 推送链路（channel layer / Redis）出问题时看门狗察觉不到——它把 pong 也算活动，
// 于是面板会一直停在旧日志上，而任务其实在跑。补拉是幂等的（偏移对齐）。
function startResyncTimer() {
  stopResyncTimer()
  resyncTimer = setInterval(async () => {
    if (!stream || Date.now() - lastFrameAt < RESYNC_QUIET_MS) return
    lastFrameAt = Date.now()
    try {
      // 状态帧可能一起丢了，所以补拉也要负责收尾
      if (await pollOnce()) finish()
    } catch { /* 下一轮再试 */ }
  }, 5000)
}

function stopResyncTimer() {
  if (resyncTimer) { clearInterval(resyncTimer); resyncTimer = null }
}

function stopFollow() {
  stream?.close()
  stream = null
  stopResyncTimer()
  stopPolling()
}

// 执行结束的收尾：只把当前记录换成落盘的完整日志，并就地刷新列表各行的状态/耗时。
// **不能走 refreshLogs()**：那会替换表格数据、重新 setCurrentRow、重新选中记录，
// 面板看着像刷新了一次，日志区还会退回第一个模块——而用户看的是刚跑完的那个模块。
async function finish() {
  dbg('finish:enter', { logId: logPanel.current?.id })
  stopFollow()
  fetchData()   // 外层任务列表（与面板选中无关）
  const taskId = logPanel.task?.id
  const logId = logPanel.current?.id
  if (!taskId) return
  const [list, detail] = await Promise.all([
    getTaskLogs(taskId),
    logId ? getTaskLogDetail(taskId, logId) : Promise.resolve(null),
  ])
  if (logPanel.task?.id !== taskId) return   // 期间换了任务：丢弃
  // 就地更新行对象（保持身份）：表格的当前行不变，也就不会触发重新选中
  const byId = new Map((list.data || []).map((r) => [r.id, r]))
  logPanel.logs.forEach((row) => Object.assign(row, byId.get(row.id) || {}))
  if (detail?.data) {
    // 合并而不是整份替换：保持 current 的对象身份，收尾这一拍就不会有 prop 身份
    // 变化传到日志组件（它按 logId 判断「换了执行记录」，一变就重置选中的模块）
    Object.assign(logPanel.current, detail.data)
    logPanel.output = detail.data.output || '（暂无输出）'
  }
  dbg('finish:done', {
    logId: logPanel.current?.id,
    steps: (logPanel.current?.steps_result || []).map((x, i) => `${i + 1}:${x.status}`).join(','),
    outLen: (logPanel.output || '').length,
  })
}

/** 拉一次输出（轮询回退与推送静默兜底共用）。返回是否已到终态。 */
async function pollOnce() {
  const taskId = logPanel.task?.id
  if (!taskId) return false
  const res = await getTaskOutput(taskId, buf.offset)
  if (logPanel.task?.id !== taskId) return false   // 期间换了任务：丢弃
  const data = res.data || {}
  // 日志流字段不能并进任务对象（否则增量片段会污染 task.output）
  const { output: _o, head: _h, start: _s, offset: _f, delta: _d,
          steps_result: steps, ...taskFields } = data
  logPanel.task = { ...logPanel.task, ...taskFields }
  if (liveTail.value) {
    if (data.delta) applyDelta(buf, data)
    else applyFull(buf, data)
    render()
  }
  if (steps?.length) applySteps(steps)   // 空数组不覆盖（[] 是真值，会抹掉模块表）
  return data.status === 'success' || data.status === 'failed'
}

function startPolling() {
  stopPolling()
  if (!logPanel.task) return
  logPanel.polling = true
  resetBuffer(buf)
  pollTimer = setInterval(async () => {
    try {
      if (await pollOnce()) {
        stopPolling()
        finish() // 执行完成：换成完整日志（不重置选中，见 finish）
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
  // 概览页的待办 / 失败告警落地：?status=paused|failed 预置状态筛选
  if (route.query.status) filter.status = String(route.query.status)
  fetchData()
  listTimer = setInterval(() => {
    if (tableData.value.some((t) => ['running', 'pending', 'paused'].includes(t.status))) {
      fetchData()
    }
  }, 5000)
  window.addEventListener('resize', updateOutputHeight)
})

onUnmounted(() => {
  stopFollow()
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
/* 模块设置区：与内容区用虚线分隔；标签定宽右对齐，两行控件左边缘对齐 */
.step-settings {
  display: flex; flex-direction: column; gap: 8px;
  padding-top: 8px; border-top: 1px dashed #e4e7ed;
}
.setting-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.setting-label { width: 60px; flex-shrink: 0; font-size: 12px; color: #909399; }
/* 行内二级标签（服务类型）不占定宽列；选中服务类型时标红提示模块被收窄 */
.setting-label.inline { width: auto; margin-left: 6px; }
.setting-label.active { color: #f56c6c; font-weight: 600; }
.setting-label.required::before { content: '*'; color: #f56c6c; margin-right: 2px; }
.match-tag { margin-left: auto; }
/* 匹配台数标红：本模块实际会执行几台，最需要一眼看清 */
.match-num { color: #f56c6c; font-weight: 600; font-size: 13px; }
.params-input :deep(textarea) { font-family: 'Courier New', monospace; font-size: 12px; }

.host-filter { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
/* 执行弹窗标题：任务名单独高亮 */
.exec-title { font-size: 16px; font-weight: 600; color: #303133; }
.exec-title-name { margin-left: 10px; font-size: 15px; font-weight: 600; color: #409eff; }
/* 任务概览条：四格等分，标签灰、值加粗 */
.exec-meta {
  display: flex; flex-wrap: wrap; align-items: center; gap: 0 18px;
  padding: 8px 12px; margin-bottom: 12px;
  border: 1px solid #ebeef5; border-radius: 4px; background: #fafafa;
}
.exec-meta-cell { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.exec-meta-cell .k { color: #909399; font-size: 12px; }
.exec-section { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 13px; }
.exec-section .muted { font-size: 12px; }
/* 执行弹窗：整体限高 80vh，标题栏/操作栏固定，正文独立滚动 */
.exec-dialog { display: flex; flex-direction: column; max-height: 80vh; box-shadow: 0 8px 24px rgba(0, 0, 0, .08); }
.exec-dialog :deep(.el-dialog__body) { flex: 1; min-height: 0; overflow-y: auto; }
/* 滚动条常显：默认细/透明，在弹窗里看不出内容可滚 */
.exec-dialog :deep(.el-dialog__body)::-webkit-scrollbar { width: 10px; }
.exec-dialog :deep(.el-dialog__body)::-webkit-scrollbar-thumb {
  background: #c0c4cc; border-radius: 5px; border: 2px solid transparent; background-clip: content-box;
}
.exec-dialog :deep(.el-dialog__body)::-webkit-scrollbar-track { background: #f5f7fa; }
.exec-dialog :deep(.el-scrollbar__bar) { opacity: 1; }
.exec-dialog :deep(.el-scrollbar__bar.is-vertical) { width: 8px; }
.exec-dialog :deep(.el-scrollbar__thumb) { background: #c0c4cc; opacity: 1; }
/* 执行弹窗：左侧模块树 + 右侧逐模块主机勾选 */
.exec-flow { display: flex; gap: 12px; margin-bottom: 14px; align-items: flex-start; }
.nav-header {
  padding: 4px 8px 8px; font-size: 12px; color: #909399;
  border-bottom: 1px solid #ebeef5; margin-bottom: 4px;
}
.flow-nav {
  width: 190px; flex-shrink: 0; padding: 6px;
  border: 1px solid #ebeef5; border-radius: 6px; background: #fafbfc;
}
.nav-node {
  display: flex; align-items: center; gap: 6px; padding: 6px 8px;
  border-radius: 4px; cursor: pointer; font-size: 13px; color: #606266;
}
.nav-node:hover { background: #f0f2f5; }
.nav-node.active { background: #ecf5ff; color: #409eff; font-weight: 600; }
.nav-root { border-top: 1px solid #ebeef5; border-radius: 0 0 4px 4px; margin-top: 4px; padding-top: 8px; }
.nav-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; background: #c0c4cc; }
.nav-dot.root { background: #409eff; }
.nav-dot.ready { background: #67c23a; }
.nav-dot.empty { background: #f56c6c; }
.nav-dot.pause { background: #e6a23c; }
.nav-order { color: #a8abb2; font-size: 11px; }
.nav-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nav-count { font-size: 12px; color: #909399; }
.exec-steps { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 16px; }
.form-steps { flex: 1; min-width: 0; }
.form-steps .step-card:last-child { margin-bottom: 0; }
/* 纵深递进：弹窗白 → 模块 #fafbfc → 缩进区 #eef1f5 → 主机表白 */
.exec-step {
  position: relative; display: flex; border: 1px solid #e4e7ed; border-radius: 6px;
  background: #fafbfc;
}
/* 模块之间短虚线相连，强调「按序执行」 */
.exec-steps > .exec-step:not(:last-child)::after {
  content: ''; position: absolute; left: 20px; bottom: -17px;
  width: 0; height: 16px; border-left: 1px dashed #c0c4cc;
}
/* 左侧序号轴：数字在顶，细线贯穿到卡片底 */
.exec-step-index {
  position: relative; width: 40px; flex-shrink: 0;
  display: flex; align-items: flex-start; justify-content: center;
  padding-top: 12px; color: #606266; font-size: 13px; font-weight: 600;
}
.exec-step-index::after {
  content: ''; position: absolute; left: 50%; top: 34px; bottom: 10px;
  width: 0; border-left: 1px solid #dcdfe6;
}
.exec-step-main { flex: 1; min-width: 0; padding: 10px 12px; }
.exec-step-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.exec-step-name { font-size: 14px; }
/* playbook 文件名降为次要信息；role 名蓝色高亮 */
.exec-step-primary { color: #a8abb2; background: transparent; padding: 0; font-size: 12px; }
.exec-step-sep { width: 1px; height: 13px; background: #dcdfe6; }
.exec-step-head .badge { font-size: 11px; color: #a8abb2; letter-spacing: .5px; }
.exec-step-role { color: #409eff; font-weight: 600; font-size: 13px; }
/* 模块服务类型：与 role 同级但用橙色区分维度 */
.exec-step-service { color: #e6a23c; font-weight: 600; font-size: 13px; }
.exec-step-service-none { color: #c0c4cc; font-size: 12px; }
.host-services { display: inline-flex; flex-wrap: wrap; gap: 4px; }
.exec-step-count { margin-left: auto; font-size: 12px; color: #909399; }
.exec-step-count .num { color: #409eff; font-size: 13px; }
.exec-step-count .num.danger { color: #f56c6c; }
/* 主机区：操作栏在滚动区之上，表格自成滚动容器 */
.exec-step-hosts { background: #eef1f5; border: 1px solid #e0e4ea; border-radius: 4px; padding: 8px; margin-top: 8px; }
/* 滚动区底部渐隐遮罩：只在有溢出时出现 */
.host-table-wrap { position: relative; }
.host-table-wrap.has-more::after {
  content: ''; position: absolute; left: 1px; right: 1px; bottom: 1px; height: 24px;
  pointer-events: none; border-radius: 0 0 4px 4px;
  background: linear-gradient(to bottom, rgba(255, 255, 255, 0), #fff);
}
/* 行高压到 38px，去掉主机名与 IP 之间的大片空档 */
.host-table :deep(.el-table__row) td { padding: 4px 0; }
.host-table :deep(.cell) { line-height: 30px; }
/* 选中态：极浅蓝底（含 hover 时保持可辨） */
.host-table :deep(.row-picked) td { background: #f5f9ff; }
.host-table :deep(.row-picked:hover) td { background: #ecf4ff; }
/* 主机名分段 */
.host-prefix { color: #a8abb2; }
.host-suffix { color: #303133; font-weight: 600; }
.exec-step-hosts .host-filter { margin-bottom: 8px; }
.exec-step-hosts .pick-btn { margin-left: auto; }
.exec-step-foot { padding: 6px 4px 0; font-size: 12px; color: #a8abb2; }
.exec-footer { display: flex; align-items: center; justify-content: space-between; }
.exec-summary {
  display: flex; align-items: center; gap: 4px; flex-wrap: wrap;
  padding: 8px 12px; border: 1px solid #ebeef5; border-radius: 4px;
  background: #fafafa; font-size: 13px; color: #606266;
}
.exec-summary > span:first-child { color: #909399; margin-right: 4px; }
.exec-summary b { color: #409eff; }
.exec-hosts-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 8px; font-size: 13px; color: #606266;
}
.flow-title { font-weight: 600; margin: 14px 0 6px; font-size: 14px; }
.flow-steps { margin: 0; padding-left: 20px; color: #606266; font-size: 13px; line-height: 1.9; }
.flow-hosts { color: #909399; font-size: 12px; line-height: 1.5; word-break: break-all; }
.step-hosts { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 3px; }
.host-chip { max-width: 100%; }
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
