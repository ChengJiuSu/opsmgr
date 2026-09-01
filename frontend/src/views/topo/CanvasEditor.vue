<!--
  拓扑画布编辑器。

  三栏：左侧调色板（加节点）| 中间 VueFlow 画布 | 右侧节点/连线详情。

  节点 id 用「客户端 key」而不是数据库主键：新拖出来的节点还没有主键，而连线必须
  立刻能引用它。key 一路带到整图 PUT，后端按 key 关联连线（见 apps/topo_mgr/graph.py）；
  已落库的节点额外在 data.dbId 里存主键，保存时回传以更新而非重建。

  只读用户（topo=read）拿到的是同一套界面，只是不渲染保存/编排/删除 —— 看图的人比
  画图的人多得多，不该为此维护第二个组件。
-->
<template>
  <div class="topo-editor" :class="{ readonly: !canWrite }">
    <!-- 顶部条 -->
    <div class="topbar">
      <div class="topbar-left">
        <el-button :icon="ArrowLeft" @click="goBack">返回列表</el-button>
        <span class="canvas-name">{{ canvas.name || '画布' }}</span>
        <template v-if="canvas.id">
          <el-tag size="small" type="info">{{ canvas.project_group_name }}</el-tag>
          <el-tag size="small" type="info">{{ canvas.channel_name }}</el-tag>
          <el-tag size="small" :type="canvas.is_prod ? 'danger' : 'success'">
            {{ canvas.environment_name }}
          </el-tag>
          <el-tag size="small" effect="plain">{{ canvas.region || '跨地域' }}</el-tag>
        </template>
        <span v-if="dirty" class="dirty">未保存</span>
      </div>
      <div class="topbar-right">
        <!-- 值班模式：只读 + 定时刷新，把这张图当大屏挂着看 -->
        <el-tooltip content="在主机节点上显示它跑哪些服务" placement="bottom">
          <el-switch
            v-model="showServices"
            active-text="服务"
            inline-prompt
            style="margin-right:4px"
            @change="onShowServicesChange"
          />
        </el-tooltip>
        <el-tooltip content="只读并每 30 秒自动刷新，异常节点高亮" placement="bottom">
          <el-switch
            v-model="watchMode"
            active-text="值班"
            inactive-text="编辑"
            inline-prompt
            style="margin-right:4px"
            @change="onWatchModeChange"
          />
        </el-tooltip>
        <el-button-group v-if="editable">
          <el-tooltip content="撤销（Ctrl+Z）" placement="bottom">
            <el-button :icon="RefreshLeft" :disabled="!canUndo" @click="undo" />
          </el-tooltip>
          <el-tooltip content="重做（Ctrl+Shift+Z）" placement="bottom">
            <el-button :icon="RefreshRight" :disabled="!canRedo" @click="redo" />
          </el-tooltip>
        </el-button-group>
        <el-button :icon="Refresh" :loading="loading" @click="reload">重新加载</el-button>
        <el-button v-if="editable" :loading="autofilling" @click="handleAutofill">
          自动编排
        </el-button>
        <el-button v-if="editable" @click="relayout">重新排版</el-button>
        <el-dropdown @command="handleMore">
          <el-button>
            更多<el-icon><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="export">导出 JSON</el-dropdown-item>
              <el-dropdown-item v-if="canCreate && !watchMode" command="clone">克隆到其它维度</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button
          v-if="editable"
          type="primary"
          :icon="Check"
          :loading="saving"
          :disabled="!dirty"
          @click="handleSave"
        >保存</el-button>
      </div>
    </div>

    <!-- 异常横幅：值班模式下这一条就是「现在要不要动手」的答案 -->
    <div v-if="watchMode" class="watch-bar" :class="issueSummary.level">
      <span v-if="issueSummary.danger">{{ issueSummary.danger }} 个节点异常</span>
      <span v-else-if="issueSummary.warning">{{ issueSummary.warning }} 个节点告警</span>
      <span v-else>全部正常</span>
      <span class="watch-meta">
        最近刷新 {{ refreshedLabel }}
        <el-button link type="primary" @click="reload">立即刷新</el-button>
      </span>
    </div>

    <div class="body">
      <!-- 左：调色板 -->
      <aside class="palette">
        <div class="palette-hint">
          点击类型加入画布（或在画布空白处右键，点哪儿生哪儿）→ 拖右侧圆点连到
          另一个节点 → 节点/连线上右键改名、删除、改关系；双击节点就地改名
        </div>
        <div class="palette-group">
          <div class="palette-group-title">平台资源</div>
          <el-button
            v-for="item in meta.ref_kinds"
            :key="item.kind"
            class="palette-item"
            :disabled="!editable"
            @click="openPicker(item)"
          >
            <el-icon><Link /></el-icon>{{ item.label }}
          </el-button>
        </div>
        <div v-for="group in meta.external_groups" :key="group.group" class="palette-group">
          <div class="palette-group-title">{{ group.group }}</div>
          <el-button
            v-for="item in group.kinds"
            :key="item.kind"
            class="palette-item"
            :disabled="!editable"
            @click="addExternalNode(item)"
          >{{ item.label }}</el-button>
        </div>
      </aside>

      <!-- 中：画布 -->
      <main v-loading="loading" class="canvas-wrap">
        <VueFlow
          v-model:nodes="nodes"
          v-model:edges="edges"
          :default-viewport="{ zoom: 0.9 }"
          :min-zoom="0.2"
          :max-zoom="2"
          :nodes-draggable="editable"
          :nodes-connectable="editable"
          :edges-updatable="editable"
          :delete-key-code="null"
          fit-view-on-init
          @connect="onConnect"
          @node-drag-start="onNodeDragStart"
          @node-drag-stop="onNodeDragStop"
          @node-click="onNodeClick"
          @node-double-click="onNodeDoubleClick"
          @node-context-menu="onNodeContextMenu"
          @edge-click="onEdgeClick"
          @edge-context-menu="onEdgeContextMenu"
          @pane-click="clearSelection"
          @pane-context-menu="onPaneContextMenu"
        >
          <Background :gap="16" pattern-color="#dfe4ec" />
          <Controls />
          <template #node-topo="{ id, data }">
            <div
              class="topo-node"
              :class="[
                `health-${data.health || 'none'}`,
                { selected: selectedNodeId === id, pulse: watchMode && data.health === 'danger' },
              ]"
            >
              <div class="node-head">
                <!-- 类型标签也可改：图上那行蓝字是给人看的分组名（「AWS · GA 加速器」），
                     真实类型由 data.kind 决定，改这行不影响任何判定 -->
                <input
                  v-if="renamingId === id && renameField === 'kindLabel'"
                  :ref="setRenameInput"
                  v-model="renameDraft"
                  class="node-rename node-rename-kind"
                  maxlength="40"
                  @mousedown.stop
                  @click.stop
                  @keydown.enter.stop.prevent="commitRename"
                  @keydown.esc.stop.prevent="cancelRename"
                  @blur="commitRename"
                />
                <span
                  v-else
                  class="node-kind"
                  :title="editable ? '双击修改类型标签' : ''"
                  @dblclick.stop="startRename(id, 'kindLabel')"
                >{{ data.kindLabel }}</span>
                <el-icon v-if="data.source === 'ref'" class="node-ref-flag"><Link /></el-icon>
              </div>
              <!-- 就地改名：双击或右键菜单进入。mousedown.stop 是必须的——
                   不拦住的话一点输入框就开始拖节点，文字根本选不中 -->
              <input
                v-if="renamingId === id && renameField === 'label'"
                :ref="setRenameInput"
                v-model="renameDraft"
                class="node-rename"
                maxlength="200"
                @mousedown.stop
                @click.stop
                @keydown.enter.stop.prevent="commitRename"
                @keydown.esc.stop.prevent="cancelRename"
                @blur="commitRename"
              />
              <div
                v-else
                class="node-label"
                @dblclick.stop="onLabelDblClick(id)"
              >{{ data.label }}</div>
              <div v-if="data.subLabel" class="node-sub">{{ data.subLabel }}</div>
              <div v-if="data.statusLabel" class="node-status">{{ data.statusLabel }}</div>
              <!-- 服务标签：注明这台机负责跑哪些服务。deployed 实心、planned 虚线，
                   一眼分清「在跑」和「还没上」。超过 3 个折叠成 +n，全铺开的话卡片
                   会比别的节点高一倍，图直接乱 -->
              <div v-if="showServices && nodeServices(data).length" class="node-services">
                <span
                  v-for="svc in nodeServices(data).slice(0, SERVICE_TAG_LIMIT)"
                  :key="svc.code"
                  class="svc-tag"
                  :class="svc.status"
                  :title="serviceTitle(svc)"
                >{{ svc.name }}×{{ svc.process_count }}</span>
                <el-tooltip
                  v-if="nodeServices(data).length > SERVICE_TAG_LIMIT"
                  placement="top"
                  :content="serviceSummary(nodeServices(data))"
                >
                  <span class="svc-tag more">+{{ nodeServices(data).length - SERVICE_TAG_LIMIT }}</span>
                </el-tooltip>
              </div>
              <Handle type="target" :position="Position.Left" />
              <Handle type="source" :position="Position.Right" />
            </div>
          </template>
        </VueFlow>
        <div v-if="!loading && !nodes.length" class="empty-hint">
          画布还是空的。点「自动编排」按维度拉出平台已有的主机 / 解析 / 证书，
          再手动补上 GA、负载均衡这类外部节点。
        </div>
      </main>

      <!-- 右：详情 -->
      <aside class="inspector">
        <template v-if="selectedNode">
          <div class="inspector-title">节点信息</div>
          <el-form label-width="72px" label-position="left" size="small">
            <el-form-item label="类型">
              <el-tag size="small">{{ selectedNode.data.kindLabelDefault }}</el-tag>
              <el-tag size="small" effect="plain" style="margin-left:4px">
                {{ selectedNode.data.source === 'ref' ? '平台资源' : '外部资源' }}
              </el-tag>
            </el-form-item>
            <!-- 图上那行蓝字。清空即恢复默认，不留一行空白 -->
            <el-form-item label="类型标签">
              <el-input
                :model-value="selectedNode.data.kindLabel"
                :placeholder="selectedNode.data.kindLabelDefault"
                :disabled="!editable"
                maxlength="40"
                clearable
                @change="onKindLabelChange"
              />
            </el-form-item>
            <el-form-item label="名称">
              <el-input
                v-if="selectedNode.data.source === 'external'"
                v-model="selectedNode.data.label"
                :disabled="!editable"
                @focus="beginTextEdit"
                @input="onTextInput"
              />
              <!-- 引用型节点的名称由平台实体决定，改这里没有意义（保存后仍以实时值展示） -->
              <span v-else class="mono">{{ selectedNode.data.label }}</span>
            </el-form-item>
            <el-form-item label="副标题">
              <el-input
                v-model="selectedNode.data.subLabel"
                :disabled="!editable || selectedNode.data.source === 'ref'"
                @focus="beginTextEdit"
                @input="onTextInput"
              />
            </el-form-item>
            <el-form-item v-if="selectedNode.data.statusLabel" label="状态">
              <el-tag size="small" :type="healthTagType(selectedNode.data.health)">
                {{ selectedNode.data.statusLabel }}
              </el-tag>
            </el-form-item>
            <!-- 为什么是黄的/红的：只给颜色不给原因，看图的人还得自己去翻三个页面 -->
            <el-form-item v-if="selectedNode.data.issues.length" label="问题">
              <div class="issues">
                <div
                  v-for="(issue, i) in selectedNode.data.issues"
                  :key="i"
                  :class="['issue', `health-${issue.level}`]"
                >{{ issue.text }}</div>
              </div>
            </el-form-item>
            <!-- 服务单列一栏：通用 extra 行只能显示「名字、名字」，
                 但这里要的是状态 + 进程名 + 进程数 -->
            <el-form-item v-if="selectedNodeServices.length" label="部署服务">
              <div class="svc-list">
                <div v-for="svc in selectedNodeServices" :key="svc.code" class="svc-row">
                  <el-tag
                    size="small"
                    :type="svc.status === 'deployed' ? 'success' : 'warning'"
                    :effect="svc.status === 'deployed' ? 'light' : 'plain'"
                  >{{ svc.name }}</el-tag>
                  <span class="svc-meta">
                    {{ svc.process_count }} 进程
                    <template v-if="svc.process_name">· {{ svc.process_name }}</template>
                  </span>
                </div>
              </div>
            </el-form-item>
            <el-form-item v-if="selectedNode.data.link" label="原始页">
              <el-link type="primary" @click="jumpTo(selectedNode.data.link)">
                在对应管理页打开
              </el-link>
            </el-form-item>
            <el-form-item label="备注">
              <el-input
                v-model="selectedNode.data.remark"
                type="textarea"
                :rows="2"
                :disabled="!editable"
                @focus="beginTextEdit"
                @input="onTextInput"
              />
            </el-form-item>
            <el-form-item label="固定位置">
              <el-switch
                v-model="selectedNode.data.pinned"
                :disabled="!editable"
                @change="onPinToggle"
              />
              <span class="tip">开启后「重新排版」不动它</span>
            </el-form-item>
          </el-form>
          <div v-if="refExtraRows.length" class="ref-extra">
            <div v-for="row in refExtraRows" :key="row.label" class="ref-extra-row">
              <span class="ref-extra-key">{{ row.label }}</span>
              <span class="ref-extra-value">{{ row.value }}</span>
            </div>
          </div>
          <el-alert
            v-if="selectedNode.data.lost"
            type="error"
            :closable="false"
            show-icon
            title="引用的平台资源已被删除，请改用其它资源或删除该节点"
            style="margin-bottom:12px"
          />
          <el-button
            v-if="editable"
            type="danger"
            plain
            style="width:100%"
            @click="removeSelectedNode"
          >删除节点</el-button>
        </template>

        <template v-else-if="selectedEdge">
          <div class="inspector-title">连线信息</div>
          <el-form label-width="72px" label-position="left" size="small">
            <el-form-item label="关系">
              <el-select
                v-model="selectedEdge.data.relation"
                :disabled="!editable"
                style="width:100%"
                @focus="relationBefore = selectedEdge.data.relation"
                @change="onRelationSelect"
              >
                <el-option
                  v-for="r in meta.relations"
                  :key="r.value"
                  :label="r.label"
                  :value="r.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="起点">
              <span class="mono">{{ nodeLabel(selectedEdge.source) }}</span>
            </el-form-item>
            <el-form-item label="终点">
              <span class="mono">{{ nodeLabel(selectedEdge.target) }}</span>
            </el-form-item>
          </el-form>
          <el-button
            v-if="editable"
            type="danger"
            plain
            style="width:100%"
            @click="removeSelectedEdge"
          >删除连线</el-button>
        </template>

        <div v-else class="inspector-empty">
          <div class="inspector-title">画布概况</div>
          <p>{{ nodes.length }} 个节点、{{ edges.length }} 条连线</p>
          <p v-if="lostCount" class="lost-warn">{{ lostCount }} 个节点引用的资源已失联</p>
          <p class="tip">点击节点或连线查看与编辑详情。</p>
        </div>
      </aside>
    </div>

    <!-- 右键菜单：节点 / 连线 / 空白处共用一个实例，菜单项按目标现算 -->
    <ContextMenu ref="menuRef" :items="menuItems" @select="onMenuSelect" />

    <!-- 所在其它画布（右键菜单里的影响面反查入口） -->
    <TopoImpactDialog
      v-model="impactVisible"
      :kind="impactTarget.kind"
      :ref-id="impactTarget.refId"
      :label="impactTarget.label"
    />

    <!-- 克隆弹窗：维度不符的主机会被后端跳过并在结果里报出来 -->
    <el-dialog v-model="cloneVisible" title="克隆画布" width="520px">
      <el-form ref="cloneFormRef" :model="cloneForm" :rules="cloneRules" label-width="100px">
        <el-form-item label="新名称" prop="name">
          <el-input v-model="cloneForm.name" />
        </el-form-item>
        <el-form-item label="新标识" prop="slug">
          <el-input v-model="cloneForm.slug" placeholder="如 ingress-path-dev" />
        </el-form-item>
        <el-form-item label="项目组">
          <el-select v-model="cloneForm.project_group" clearable style="width:100%"
                     placeholder="不改（沿用原画布）">
            <el-option v-for="i in dicts.projectGroups" :key="i.id" :label="i.name" :value="i.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="渠道">
          <el-select v-model="cloneForm.channel" clearable style="width:100%"
                     placeholder="不改（沿用原画布）">
            <el-option v-for="i in dicts.channels" :key="i.id" :label="i.name" :value="i.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="环境">
          <el-select v-model="cloneForm.environment" clearable style="width:100%"
                     placeholder="不改（沿用原画布）">
            <el-option v-for="i in dicts.environments" :key="i.id" :label="i.name" :value="i.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="地域">
          <el-input v-model="cloneForm.region" placeholder="留空 = 跨地域" />
        </el-form-item>
      </el-form>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="换了维度时，与新维度不符的主机节点会被跳过（域名/解析/证书照常保留），结果里会列出跳过了哪些——GA、负载均衡这层骨架才是克隆真正要复用的部分。"
      />
      <template #footer>
        <el-button @click="cloneVisible = false">取消</el-button>
        <el-button type="primary" :loading="cloning" @click="submitClone">克隆</el-button>
      </template>
    </el-dialog>

    <!-- 平台资源选择弹窗 -->
    <el-dialog v-model="pickerVisible" :title="`选择${picker.label}`" width="620px">
      <el-input
        v-model="picker.keyword"
        placeholder="按名称搜索"
        clearable
        style="margin-bottom:12px"
        @input="pickerSearchDebounced"
        @keyup.enter="loadCandidates"
        @clear="loadCandidates"
      />
      <el-table
        v-loading="picker.loading"
        :data="picker.items"
        height="360"
        stripe
        @row-click="pickCandidate"
      >
        <el-table-column prop="label" label="名称" min-width="200" />
        <el-table-column prop="sub_label" label="信息" min-width="160" />
        <el-table-column prop="extra" label="附加" min-width="120" show-overflow-tooltip />
        <el-table-column label="" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.on_canvas" size="small" type="info">已在图上</el-tag>
            <el-button v-else size="small" type="primary" link>加入</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { VueFlow, Handle, Position, MarkerType, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import {
  ArrowLeft, ArrowDown, Check, Refresh, RefreshLeft, RefreshRight, Link,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import dagre from 'dagre'
import { debounce } from '../../utils/debounce'
import { useAuthStore } from '../../stores/auth'
import ContextMenu from '../../components/ContextMenu.vue'
import TopoImpactDialog from '../../components/TopoImpactDialog.vue'
import {
  projectGroupApi, channelApi, environmentApi, fetchDictOptions,
} from '../../api/assets'
import {
  getGraph, saveGraph, autofillCanvas, getCandidates, getTopoMeta,
  cloneCanvas, exportCanvas,
} from '../../api/topo'

import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const canWrite = computed(() => authStore.can('topo', 'write'))
// 克隆是「另存一张新画布」= 新增，read_create 档就该给
const canCreate = computed(() => authStore.can('topo', 'create'))
const canvasId = computed(() => route.params.id)

// getNodes 是画布的权威状态：拖拽后的坐标先落在 store 里，v-model 回写虽然也会同步，
// 但保存时直接读 store 就不必依赖那次回写的时机
// screenToFlowCoordinate：把鼠标屏幕坐标换成画布坐标，右键新建的节点才能「点哪儿生哪儿」
const { fitView, getNodes, screenToFlowCoordinate } = useVueFlow()

//: 主机卡片上最多铺几个服务标签，多的收成 +n。一台机六个服务全铺开，卡片会比
//: 别的节点高一倍，整张图的行距就乱了
const SERVICE_TAG_LIMIT = 3
//: 「显示服务」是按人记的看图偏好，存 localStorage——与概览页区块显隐同口径，
//: 不进后端：它不影响任何人看到的数据，只影响这台浏览器上卡片有多高
const SHOW_SERVICES_KEY = 'topo.showServices'

//: 值班模式的刷新间隔。30 秒是「大屏挂着看」的量级：再快只是给库加压，
//: 主机状态与证书天数都不是秒级变化的东西
const WATCH_INTERVAL = 30000

const loading = ref(false)
const saving = ref(false)
const autofilling = ref(false)
const dirty = ref(false)
const canvas = ref({})
const nodes = ref([])
const edges = ref([])
const meta = reactive({ ref_kinds: [], external_groups: [], relations: [] })
const selectedNodeId = ref('')
const selectedEdgeId = ref('')
// 值班模式：只读 + 定时刷新。初值取自 URL，所以「大屏那份链接」可以直接分享
const watchMode = ref(route.query.watch === '1')
const showServices = ref(readShowServices())
const refreshedAt = ref(null)
// 右键菜单与就地改名
const menuRef = ref(null)
const menuItems = ref([])
// 菜单作用对象：{ type: 'node'|'edge'|'pane', id?, position? }
const menuTarget = ref({ type: 'pane' })
const renamingId = ref('')
// 就地编辑的目标字段：'label'（名称）| 'kindLabel'（蓝色那行类型标签）
const renameField = ref('label')
const renameDraft = ref('')
const impactVisible = ref(false)
const impactTarget = ref({ kind: 'server', refId: null, label: '' })
let keySeq = 0
let watchTimer = null
let renameInputEl = null

// 外部节点的默认落点：避开左上角已有节点，叠一层偏移防止新节点互相压住
const NEW_NODE_ORIGIN = { x: 120, y: 40 }
const RELATION_LABELS = computed(() =>
  Object.fromEntries(meta.relations.map((r) => [r.value, r.label])))

// 能不能改：有写权限**且**不在值班模式。值班模式刻意连有权限的人也锁住——
// 一张定时自动刷新的图上做编辑，下一次刷新就把你的改动冲了
const editable = computed(() => canWrite.value && !watchMode.value)

const selectedNode = computed(() => nodes.value.find((n) => n.id === selectedNodeId.value))
const selectedEdge = computed(() => edges.value.find((e) => e.id === selectedEdgeId.value))
const lostCount = computed(() => nodes.value.filter((n) => n.data.lost).length)
// 主机节点上的服务清单。实时读来的，存在 ref.extra 里（见 graph._server_services）
function nodeServices(data) {
  return data?.extra?.services || []
}

function serviceTitle(svc) {
  const parts = [`${svc.name}×${svc.process_count}`, svc.status_label]
  if (svc.process_name) parts.push(svc.process_name)
  return parts.join(' · ')
}

function serviceSummary(services) {
  return services.map(serviceTitle).join('\n')
}

const selectedNodeServices = computed(() => nodeServices(selectedNode.value?.data))

function readShowServices() {
  try {
    const saved = localStorage.getItem(SHOW_SERVICES_KEY)
    return saved === null ? true : saved === '1'
  } catch {
    // 隐私模式 / 禁用站点数据时读不到，默认开着
    return true
  }
}

function onShowServicesChange(value) {
  try {
    localStorage.setItem(SHOW_SERVICES_KEY, value ? '1' : '0')
  } catch {
    // 存不下就只在本次会话生效，不值得为此报错
  }
}

const canUndo = computed(() => editable.value && undoStack.value.length > 0)
const canRedo = computed(() => editable.value && redoStack.value.length > 0)

// 引用型节点的实时附加信息（主机 IP / 服务、证书域名等），按 kind 挑要展示的几项
const REF_EXTRA_LABELS = {
  private_ip: '私网 IP', public_ip: '公网 IP', region: '地域',
  cloud_provider: '云厂商', record_count: '解析记录数',
  type: '记录类型', value: '记录值', line: '解析线路',
  domains: '证书域名', expires_at: '过期时间',
}
//: 这几个键有自己的渲染，不能落进通用「键: 值」行里
const REF_EXTRA_SKIP = new Set(['services', 'cloud_provider_code'])
const refExtraRows = computed(() => {
  const extra = selectedNode.value?.data?.extra || {}
  return Object.entries(extra)
    .filter(([key]) => !REF_EXTRA_SKIP.has(key))
    .filter(([, value]) => value !== '' && value !== null && value !== undefined
      && !(Array.isArray(value) && !value.length))
    .map(([key, value]) => ({
      label: REF_EXTRA_LABELS[key] || key,
      value: Array.isArray(value) ? value.join('、') : String(value),
    }))
})

/** 图上的异常汇总：横幅与「立即刷新」共用，不再单独拉一次健康度接口。 */
const issueSummary = computed(() => {
  let danger = 0
  let warning = 0
  nodes.value.forEach((n) => {
    if (n.data.health === 'danger') danger += 1
    else if (n.data.health === 'warning') warning += 1
  })
  return {
    danger,
    warning,
    level: danger ? 'danger' : (warning ? 'warning' : 'ok'),
  }
})

const refreshedLabel = computed(() => (refreshedAt.value
  ? new Date(refreshedAt.value).toLocaleTimeString('zh-CN', { hour12: false })
  : '—'))

function stopWatchTimer() {
  if (watchTimer) {
    clearInterval(watchTimer)
    watchTimer = null
  }
}

function startWatchTimer() {
  stopWatchTimer()
  watchTimer = setInterval(() => {
    // 有未保存改动、或正在改名时不自动刷新：把人正在画/正在写的东西冲掉
    // 是最不能接受的
    if (!dirty.value && !renamingId.value) reload()
  }, WATCH_INTERVAL)
}

async function onWatchModeChange(value) {
  if (value && dirty.value) {
    try {
      await ElMessageBox.confirm(
        '进入值班模式会变成只读并定时刷新，当前未保存的改动会丢弃。继续？',
        '提示', { type: 'warning' })
    } catch {
      watchMode.value = false
      return
    }
    dirty.value = false
    await reload()
  }
  // URL 跟着变：值班大屏那份链接可以直接分享，刷新浏览器也还在值班模式
  router.replace({
    query: { ...route.query, watch: value ? '1' : undefined },
  })
  if (value) {
    clearSelection()
    startWatchTimer()
  } else {
    stopWatchTimer()
  }
}

// ── 撤销 / 重做 ─────────────────────────────────────────
//
// 快照式而不是命令式：图上就几十个节点，整份深拷贝的代价可以忽略，而命令式要为
// 每种操作写一对 do/undo，删节点还得记住它身上的连线——写十种就有十种写错的机会。
//
// 栈底是「刚从服务端加载/保存完」的那一份，所以撤销栈空 = 回到了那个状态，
// dirty 可以直接跟着栈深走，不用另猜。

const HISTORY_LIMIT = 50
const undoStack = ref([])
const redoStack = ref([])
// 拖拽的前一态在 drag-start 就得抓（drag-stop 时坐标已经变了），
// 但只有真的挪动过才入栈，否则一次单击也会留下一条历史
let dragSnapshot = null
// 文本框的前一态：聚焦时暂存，真的敲了字才入栈——点一下输入框不该留下一条空历史
let textEditSnapshot = null

/** 当前图的快照。坐标从 VueFlow store 取——拖拽后的位置先落在那里。 */
function snapshot() {
  const placed = new Map(getNodes.value.map((n) => [n.id, n.position]))
  return {
    nodes: nodes.value.map((n) => ({
      id: n.id,
      type: n.type,
      position: { ...(placed.get(n.id) || n.position) },
      data: JSON.parse(JSON.stringify(n.data)),
    })),
    edges: edges.value.map((e) => ({ ...e, data: { ...e.data } })),
  }
}

/** 记一步。必须在改动**之前**调用：栈里存的是「改之前长什么样」。 */
function pushHistory(snap = null) {
  undoStack.value.push(snap || snapshot())
  if (undoStack.value.length > HISTORY_LIMIT) undoStack.value.shift()
  // 新的改动会让原来的重做分支失效，与所有编辑器一致
  redoStack.value = []
}

function restore(snap) {
  // 节点对象整份换掉，指向旧对象的改名态与菜单必须先收
  cancelRename()
  menuRef.value?.close()
  clearSelection()
  nodes.value = snap.nodes
  edges.value = snap.edges
  // 刻意不 fitView：人正盯着某一块看，撤销时视口跳一下比不跳更难受
}

function clearHistory() {
  undoStack.value = []
  redoStack.value = []
  dragSnapshot = null
  textEditSnapshot = null
}

function undo() {
  if (!editable.value || !undoStack.value.length) return
  redoStack.value.push(snapshot())
  restore(undoStack.value.pop())
  // 栈空 = 回到了加载/保存时那一份，此时「未保存」这个标记就该消失
  dirty.value = undoStack.value.length > 0
}

function redo() {
  if (!editable.value || !redoStack.value.length) return
  undoStack.value.push(snapshot())
  restore(redoStack.value.pop())
  dirty.value = true
}

function markDirty() {
  dirty.value = true
}

// 下拉打开前的关系值：el-select 的 @change 只给新值，撤销要的是旧值
const relationBefore = ref('')
function beginTextEdit() {
  textEditSnapshot = snapshot()
}

function onTextInput() {
  if (textEditSnapshot) {
    pushHistory(textEditSnapshot)
    textEditSnapshot = null   // 一次聚焦只记一步，不是每敲一个字记一步
  }
  markDirty()
}

function onRelationSelect(value) {
  onRelationChange(value, relationBefore.value || null)
}

/** 右侧面板的「固定位置」开关。v-model 先改了值，所以快照按取反重建那一份。 */
function onPinToggle(value) {
  const node = selectedNode.value
  if (!node) return
  const snap = snapshot()
  const hit = snap.nodes.find((n) => n.id === node.id)
  if (hit) hit.data.pinned = !value
  pushHistory(snap)
  markDirty()
}

function healthTagType(health) {
  return { healthy: 'success', warning: 'warning', danger: 'danger' }[health] || 'info'
}

function nodeLabel(id) {
  return nodes.value.find((n) => n.id === id)?.data?.label || id
}

function clearSelection() {
  selectedNodeId.value = ''
  selectedEdgeId.value = ''
}

function onNodeClick({ node }) {
  selectedEdgeId.value = ''
  selectedNodeId.value = node.id
}

function onEdgeClick({ edge }) {
  selectedNodeId.value = ''
  selectedEdgeId.value = edge.id
}

function jumpTo(link) {
  router.push({ path: link.path, query: link.query || {} })
}

// ── 取数 ────────────────────────────────────────────────

/** 后端节点 → VueFlow 节点。key 用 `n<主键>`，与新建节点的 `new-<n>` 不会撞。 */
function toFlowNode(item) {
  return {
    id: `n${item.id}`,
    type: 'topo',
    position: { x: item.x, y: item.y },
    data: {
      dbId: item.id,
      source: item.source,
      kind: item.kind,
      // 展示用标签：优先图上存的自定义值，否则用后端登记表给的默认名
      kindLabel: (item.attrs || {}).kind_label || item.kind_label,
      kindLabelDefault: item.kind_label,
      refId: item.ref_id,
      label: item.label,
      subLabel: item.sub_label,
      remark: item.remark,
      attrs: item.attrs || {},
      pinned: item.pinned,
      health: item.ref?.health || 'none',
      statusLabel: item.ref?.status_label || '',
      link: item.ref?.link || null,
      extra: item.ref?.extra || {},
      issues: item.ref?.issues || [],
      lost: item.ref ? item.ref.exists === false : false,
    },
  }
}

function toFlowEdge(item) {
  return buildEdge(`e${item.id}`, `n${item.source}`, `n${item.target}`, item.relation)
}

function buildEdge(id, source, target, relation) {
  return {
    id,
    source,
    target,
    label: RELATION_LABELS.value[relation] || relation,
    markerEnd: MarkerType.ArrowClosed,
    data: { relation },
  }
}

function applyGraph(data) {
  // 整图替换会换掉所有节点对象，改名态指向的那个对象随之作废
  cancelRename()
  menuRef.value?.close()
  // 服务端刚给的这一份是新基线：撤到这里就到底了，再往前撤没有意义
  clearHistory()
  canvas.value = data.canvas || canvas.value
  nodes.value = (data.nodes || []).map(toFlowNode)
  edges.value = (data.edges || []).map(toFlowEdge)
  dirty.value = false
  refreshedAt.value = Date.now()
  clearSelection()
  nextTick(() => fitView({ padding: 0.2 }))
}

async function reload() {
  if (dirty.value && !watchMode.value) {
    try {
      await ElMessageBox.confirm('画布有未保存的改动，重新加载会丢弃它们。继续？', '提示',
        { type: 'warning' })
    } catch {
      return
    }
  }
  loading.value = true
  try {
    const res = await getGraph(canvasId.value)
    applyGraph(res.data)
  } finally {
    loading.value = false
  }
}

// ── 加节点 ──────────────────────────────────────────────

function nextKey() {
  keySeq += 1
  return `new-${keySeq}`
}

function freeSpot() {
  const offset = nodes.value.length * 24
  return { x: NEW_NODE_ORIGIN.x + (offset % 240), y: NEW_NODE_ORIGIN.y + offset }
}

function addExternalNode(kind, position = null) {
  pushHistory()
  const id = nextKey()
  nodes.value.push({
    id,
    type: 'topo',
    // 右键新建时用光标处的画布坐标，点哪儿生哪儿；从左侧调色板加则退回错开落点
    position: position || freeSpot(),
    data: {
      dbId: null, source: 'external', kind: kind.kind, kindLabel: kind.label,
      kindLabelDefault: kind.label,
      refId: null, label: `新${kind.label}`, subLabel: '', remark: '',
      attrs: {}, pinned: false, health: 'none', statusLabel: '',
      link: null, extra: {}, issues: [], lost: false,
    },
  })
  selectedEdgeId.value = ''
  selectedNodeId.value = id
  markDirty()
  // 新节点默认名是「新XX」，直接进改名态：加一个节点后要做的事十有八九是给它起名
  nextTick(() => startRename(id, 'label'))
}

const pickerVisible = ref(false)
const picker = reactive({ kind: '', label: '', keyword: '', items: [], loading: false })

function openPicker(kind) {
  picker.kind = kind.kind
  picker.label = kind.label
  picker.keyword = ''
  picker.items = []
  pickerVisible.value = true
  loadCandidates()
}

async function loadCandidates() {
  pickerSearchDebounced.cancel()
  picker.loading = true
  try {
    const res = await getCandidates(canvasId.value,
      { kind: picker.kind, keyword: picker.keyword })
    // 已在图上的资源也列出来（标记「已在图上」），否则用户会怀疑是搜不到
    picker.items = res.data?.results || []
  } finally {
    picker.loading = false
  }
}
const pickerSearchDebounced = debounce(loadCandidates)
onUnmounted(() => pickerSearchDebounced.cancel())

function pickCandidate(row) {
  if (row.on_canvas) {
    ElMessage.warning('该资源已在画布上')
    return
  }
  pushHistory()
  const id = nextKey()
  nodes.value.push({
    id,
    type: 'topo',
    position: freeSpot(),
    data: {
      dbId: null, source: 'ref', kind: picker.kind, kindLabel: picker.label,
      kindLabelDefault: picker.label,
      refId: row.ref_id, label: row.label, subLabel: row.sub_label || '',
      remark: '', attrs: {}, pinned: false, health: 'none',
      statusLabel: '保存后显示实时状态', link: null, extra: {}, issues: [],
      lost: false,
    },
  })
  row.on_canvas = true
  selectedEdgeId.value = ''
  selectedNodeId.value = id
  markDirty()
}

// ── 就地改名 ────────────────────────────────────────────

function setRenameInput(el) {
  renameInputEl = el
  if (el) nextTick(() => { el.focus(); el.select() })
}

/** 双击名称：外部节点就地改名，引用型节点去它的原始页（名字本来就该在那边改）。 */
function onLabelDblClick(nodeId) {
  const node = nodes.value.find((n) => n.id === nodeId)
  if (!node) return
  if (node.data.source === 'ref') {
    if (node.data.link) jumpTo(node.data.link)
    return
  }
  startRename(nodeId, 'label')
}

function startRename(nodeId, field = 'label') {
  const node = nodes.value.find((n) => n.id === nodeId)
  if (!node || !editable.value) return
  // 名称：引用型节点的名字取自平台实体，在这里改了保存后仍会被实时值覆盖，
  // 直接把人送去能真正改名的那个页面。类型标签只是给人看的分组名，两类都能改
  if (field === 'label' && node.data.source === 'ref') {
    ElMessage.info('该节点的名称取自平台资源，请在对应管理页修改')
    return
  }
  selectedEdgeId.value = ''
  selectedNodeId.value = nodeId
  renameField.value = field
  renameDraft.value = field === 'kindLabel' ? node.data.kindLabel : node.data.label
  renamingId.value = nodeId
}

function commitRename() {
  const nodeId = renamingId.value
  const field = renameField.value
  if (!nodeId) return
  const node = nodes.value.find((n) => n.id === nodeId)
  renamingId.value = ''
  renameInputEl = null
  const value = renameDraft.value.trim()
  if (!node) return
  if (field === 'kindLabel') {
    // 清空 = 恢复后端登记的默认标签，而不是留一行空白蓝字
    setKindLabel(node, value)
    return
  }
  if (!value) {
    ElMessage.warning('节点名称不能为空，已保留原名')
    return
  }
  if (value === node.data.label) return
  pushHistory()
  node.data.label = value
  markDirty()
}

/** 右侧面板改类型标签：用 @change（失焦/回车）而不是 @input——
    每敲一个字入一次历史栈的话，撤销要按几十下。 */
function onKindLabelChange(value) {
  if (selectedNode.value) setKindLabel(selectedNode.value, value)
}

/** 写自定义类型标签。空值 = 清掉覆盖，回到 `/api/topo/meta/` 给的那个名字。

    存在 attrs.kind_label 里而不是新加一列：attrs 就是为「图上这个节点自己的展示
    细节」留的自由字段，加一列意味着一次迁移 + 序列化器 + 导出格式变更，而这个值
    不参与任何判定（真实类型始终是 data.kind）。 */
function setKindLabel(node, value) {
  const next = (value || '').trim()
  const current = node.data.attrs?.kind_label || ''
  if (next === current) return
  pushHistory()
  const attrs = { ...(node.data.attrs || {}) }
  if (next) attrs.kind_label = next
  else delete attrs.kind_label
  node.data.attrs = attrs
  node.data.kindLabel = next || node.data.kindLabelDefault
  markDirty()
}

function cancelRename() {
  renamingId.value = ''
  renameInputEl = null
}

// ── 右键菜单 ────────────────────────────────────────────

/** 节点菜单。只读时只留「看」的动作——给一堆点了没反应的菜单项比不给更糟。 */
function nodeMenuItems(node) {
  const related = edges.value.filter(
    (e) => e.source === node.id || e.target === node.id).length
  const isRef = node.data.source === 'ref'
  const items = []
  if (editable.value) {
    items.push({
      key: 'rename', label: '重命名',
      disabled: isRef, hint: isRef ? '取自平台资源' : '',
    })
    items.push({ key: 'rename-kind', label: '修改类型标签' })
    if (node.data.attrs?.kind_label) {
      items.push({ key: 'reset-kind', label: '恢复默认类型标签' })
    }
    items.push({
      key: 'pin', label: node.data.pinned ? '取消固定位置' : '固定位置',
      checked: node.data.pinned,
    })
  }
  if (isRef && node.data.link) {
    items.push({ key: 'open', label: '打开原始页' })
  }
  if (isRef) {
    items.push({ key: 'impact', label: '查看所在其它画布' })
  }
  items.push({ key: 'copy', label: '复制名称' })
  if (editable.value) {
    items.push({ divider: true })
    items.push({
      key: 'detach', label: `断开所有连线（${related}）`,
      disabled: !related, hint: related ? '' : '没有连线',
    })
    items.push({ key: 'delete', label: '删除节点', danger: true })
  }
  return items
}

/** 连线菜单：7 种关系直接平铺，改关系不用再绕右侧下拉。 */
function edgeMenuItems(edge) {
  if (!editable.value) return [{ key: 'noop', label: '值班模式下只读', disabled: true }]
  return [
    {
      key: 'relation', label: '改关系',
      children: meta.relations.map((r) => ({
        key: `relation:${r.value}`, label: r.label,
        checked: edge.data.relation === r.value,
      })),
    },
    { divider: true },
    { key: 'delete-edge', label: '删除连线', danger: true },
  ]
}

/** 空白处菜单：在光标处新建节点 + 视图操作。 */
function paneMenuItems() {
  const items = []
  if (editable.value) {
    items.push({
      key: 'undo', label: '撤销', disabled: !canUndo.value,
      hint: canUndo.value ? '' : '没有可撤销的操作',
    })
    items.push({
      key: 'redo', label: '重做', disabled: !canRedo.value,
      hint: canRedo.value ? '' : '没有可重做的操作',
    })
    items.push({ divider: true })
    items.push({
      key: 'add', label: '在此处新建节点',
      children: meta.external_groups.flatMap((group) => group.kinds.map((k) => ({
        key: `add:${k.kind}`, label: `${group.group} · ${k.label}`,
      }))),
    })
    items.push({ key: 'relayout', label: '重新排版' })
    items.push({ divider: true })
  }
  items.push({ key: 'fit', label: '适应视图' })
  return items
}

function openMenu(event, target, items) {
  if (!items.length) return
  event.preventDefault()
  // 菜单弹出前先收掉就地编辑：编辑框失焦会提交，避免菜单动作和改名互相盖
  if (renamingId.value) commitRename()
  menuTarget.value = target
  menuItems.value = items
  menuRef.value?.open(event)
}

function onNodeContextMenu({ event, node }) {
  selectedEdgeId.value = ''
  selectedNodeId.value = node.id
  openMenu(event, { type: 'node', id: node.id }, nodeMenuItems(node))
}

function onEdgeContextMenu({ event, edge }) {
  selectedNodeId.value = ''
  selectedEdgeId.value = edge.id
  openMenu(event, { type: 'edge', id: edge.id }, edgeMenuItems(edge))
}

function onPaneContextMenu(event) {
  // 落点用画布坐标而不是屏幕坐标：缩放/平移过之后两者差得很远
  const position = screenToFlowCoordinate({ x: event.clientX, y: event.clientY })
  openMenu(event, { type: 'pane', position }, paneMenuItems())
}

function onNodeDoubleClick({ node }) {
  if (node.data.source === 'ref' && node.data.link) {
    jumpTo(node.data.link)
    return
  }
  startRename(node.id)
}

async function onMenuSelect(item) {
  const target = menuTarget.value
  const node = target.type === 'node'
    ? nodes.value.find((n) => n.id === target.id) : null
  const edge = target.type === 'edge'
    ? edges.value.find((e) => e.id === target.id) : null

  if (item.key.startsWith('add:')) {
    addExternalNode(findKind(item.key.slice(4)), target.position)
    return
  }
  if (item.key.startsWith('relation:') && edge) {
    if (edge.data.relation !== item.key.slice(9)) {
      pushHistory()
      edge.data.relation = item.key.slice(9)
      onRelationChange(edge.data.relation)
    }
    return
  }
  switch (item.key) {
    case 'rename':
      startRename(target.id, 'label')
      break
    case 'rename-kind':
      startRename(target.id, 'kindLabel')
      break
    case 'reset-kind':
      if (node) setKindLabel(node, '')
      break
    case 'pin':
      if (node) {
        pushHistory()
        node.data.pinned = !node.data.pinned
        markDirty()
      }
      break
    case 'open':
      if (node?.data.link) jumpTo(node.data.link)
      break
    case 'impact':
      if (node) {
        impactTarget.value = {
          kind: node.data.kind, refId: node.data.refId, label: node.data.label,
        }
        impactVisible.value = true
      }
      break
    case 'copy':
      if (node) await copyText(node.data.label)
      break
    case 'detach':
      if (node) detachNode(node)
      break
    case 'delete':
      if (node) await removeNode(node)
      break
    case 'delete-edge':
      if (edge) removeEdge(edge)
      break
    case 'undo':
      undo()
      break
    case 'redo':
      redo()
      break
    case 'relayout':
      relayout()
      break
    case 'fit':
      fitView({ padding: 0.2 })
      break
    default:
      break
  }
}

function findKind(kind) {
  for (const group of meta.external_groups) {
    const hit = group.kinds.find((k) => k.kind === kind)
    if (hit) return hit
  }
  return { kind, label: kind }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    // 非 HTTPS / 无剪贴板权限时降级：把名字显示出来让人手动复制，
    // 静默失败会让人以为复制成功了
    ElMessageBox.alert(text, '手动复制', { type: 'info' })
  }
}

function detachNode(node) {
  const before = edges.value.length
  const snap = snapshot()
  edges.value = edges.value.filter(
    (e) => e.source !== node.id && e.target !== node.id)
  const removed = before - edges.value.length
  if (removed) {
    pushHistory(snap)  // 一条没断就不记：菜单里那项本来也会置灰
    markDirty()
    ElMessage.success(`已断开 ${removed} 条连线`)
  }
}

// ── 连线与删除 ──────────────────────────────────────────

function onConnect(params) {
  const relation = 'custom'
  const exists = edges.value.some(
    (e) => e.source === params.source && e.target === params.target
      && e.data.relation === relation)
  if (exists) {
    ElMessage.warning('这两个节点之间已有同类型连线，请改用其它关系')
    return
  }
  pushHistory()
  const id = `new-edge-${params.source}-${params.target}-${Date.now()}`
  edges.value.push(buildEdge(id, params.source, params.target, relation))
  selectedNodeId.value = ''
  selectedEdgeId.value = id
  markDirty()
}

function onRelationChange(value, previous = null) {
  const edge = selectedEdge.value
  if (!edge) return
  if (previous !== null) {
    // 右侧下拉走这条路：v-model 已经把 relation 改掉了，快照里塞回旧值
    const snap = snapshot()
    const hit = snap.edges.find((e) => e.id === edge.id)
    if (hit) hit.data.relation = previous
    pushHistory(snap)
  }
  edge.label = RELATION_LABELS.value[value] || value
  markDirty()
}

function onNodeDragStart() {
  dragSnapshot = snapshot()
}

function onNodeDragStop() {
  const before = dragSnapshot
  dragSnapshot = null
  if (!before) {
    markDirty()
    return
  }
  const now = new Map(getNodes.value.map((n) => [n.id, n.position]))
  const moved = before.nodes.some((n) => {
    const pos = now.get(n.id)
    return pos && (Math.round(pos.x) !== Math.round(n.position.x)
      || Math.round(pos.y) !== Math.round(n.position.y))
  })
  // 单击节点也会走 drag-start/stop：没挪动就不该留下一条历史，也不该标未保存
  if (!moved) return
  pushHistory(before)
  markDirty()
}

/** 删一个节点。带连线才二次确认：孤立节点删错了再加一个就是，
    带连线的节点删掉会静默改变链路形状，那种错不容易发现。 */
async function removeNode(node) {
  const related = edges.value.filter((e) => e.source === node.id || e.target === node.id)
  if (related.length) {
    await ElMessageBox.confirm(
      `删除节点「${node.data.label}」会同时删掉与它相连的 ${related.length} 条连线。继续？`,
      '提示', { type: 'warning' })
  }
  pushHistory()  // 确认之后再记，取消掉的确认不该留下一条历史
  edges.value = edges.value.filter((e) => e.source !== node.id && e.target !== node.id)
  nodes.value = nodes.value.filter((n) => n.id !== node.id)
  if (renamingId.value === node.id) cancelRename()
  clearSelection()
  markDirty()
}

function removeEdge(edge) {
  pushHistory()
  edges.value = edges.value.filter((e) => e.id !== edge.id)
  clearSelection()
  markDirty()
}

async function removeSelectedNode() {
  if (selectedNode.value) await removeNode(selectedNode.value)
}

function removeSelectedEdge() {
  if (selectedEdge.value) removeEdge(selectedEdge.value)
}

/** Delete / Backspace 删选中的节点或连线。

    自己接而不是交给 vue-flow 的 deleteKeyCode：那条路不走确认框、也不会把改动
    标成未保存。输入框里按退格当然不能删节点，所以先看事件源。 */
async function onKeydown(event) {
  const tag = (event.target?.tagName || '').toLowerCase()
  const typing = tag === 'input' || tag === 'textarea' || event.target?.isContentEditable

  // 撤销/重做：输入框里交给浏览器自己的文本撤销，别抢
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z' && !typing) {
    event.preventDefault()
    if (event.shiftKey) redo()
    else undo()
    return
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y' && !typing) {
    event.preventDefault()
    redo()
    return
  }

  if (event.key !== 'Delete' && event.key !== 'Backspace') return
  if (!editable.value || renamingId.value || typing) return
  if (selectedNode.value) {
    event.preventDefault()
    try {
      await removeNode(selectedNode.value)
    } catch {
      // 确认框取消
    }
  } else if (selectedEdge.value) {
    event.preventDefault()
    removeEdge(selectedEdge.value)
  }
}

// ── 自动排版（dagre，从左到右） ─────────────────────────

/** 节点卡片的估算高度，只给 dagre 布点用。 */
function nodeHeight(node) {
  const base = 78
  if (!showServices.value || !nodeServices(node.data).length) return base
  return base + 22
}

function relayout() {
  if (!nodes.value.length) return
  pushHistory()
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'LR', nodesep: 40, ranksep: 90 })
  // 高度要跟着卡片实际高度走：服务标签行让主机卡片高一截，按固定值布点会叠在一起
  nodes.value.forEach((n) => g.setNode(n.id, {
    width: 220,
    height: nodeHeight(n),
  }))
  edges.value.forEach((e) => g.setEdge(e.source, e.target))
  dagre.layout(g)
  nodes.value = nodes.value.map((n) => {
    // 钉住的节点不动：人手摆的位置通常带着说不出口的信息（这条链路要横着看）
    if (n.data.pinned) return n
    const laid = g.node(n.id)
    return laid
      ? { ...n, position: { x: laid.x - 110, y: laid.y - nodeHeight(n) / 2 } }
      : n
  })
  markDirty()
  nextTick(() => fitView({ padding: 0.2 }))
}

// ── 保存与自动编排 ──────────────────────────────────────

async function handleSave() {
  saving.value = true
  try {
    const placed = new Map(getNodes.value.map((n) => [n.id, n.position]))
    const payload = {
      layout_mode: 'manual',
      nodes: nodes.value.map((n) => ({
        key: n.id,
        id: n.data.dbId,
        source: n.data.source,
        kind: n.data.kind,
        ref_id: n.data.refId,
        label: n.data.label,
        sub_label: n.data.source === 'ref' ? '' : n.data.subLabel,
        remark: n.data.remark,
        attrs: n.data.attrs,
        pinned: n.data.pinned,
        x: Math.round((placed.get(n.id) || n.position).x),
        y: Math.round((placed.get(n.id) || n.position).y),
      })),
      edges: edges.value.map((e) => ({
        source: e.source,
        target: e.target,
        relation: e.data.relation,
      })),
    }
    const res = await saveGraph(canvasId.value, payload)
    // 用后端回传的整图刷新：新节点在这里第一次拿到主键，引用型节点也拿到实时状态
    applyGraph(res.data)
    ElMessage.success(res.msg || '保存成功')
  } finally {
    saving.value = false
  }
}

async function handleAutofill() {
  if (dirty.value) {
    ElMessage.warning('请先保存当前改动，再执行自动编排')
    return
  }
  await ElMessageBox.confirm(
    '将按本画布的项目组/渠道/环境（含地域）拉取平台已有的服务器、解析记录、主域名与证书，' +
    '生成节点与关系。已在图上的资源会跳过，手动摆好的节点不会被移动。',
    '自动编排', { type: 'info' })
  autofilling.value = true
  try {
    const res = await autofillCanvas(canvasId.value)
    if (res.data?.nodes) applyGraph({ ...res.data, canvas: canvas.value })
    ElMessage.success(res.msg || '编排完成')
  } finally {
    autofilling.value = false
  }
}

// ── 导出 / 克隆 ─────────────────────────────────────────

const dicts = reactive({ projectGroups: [], channels: [], environments: [] })
const cloneVisible = ref(false)
const cloning = ref(false)
const cloneFormRef = ref()
const cloneForm = reactive({
  name: '', slug: '', project_group: null, channel: null, environment: null, region: '',
})
const cloneRules = {
  name: [{ required: true, message: '请输入新画布名称' }],
  slug: [
    { required: true, message: '请输入新画布标识' },
    { pattern: /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/, message: '只能用小写字母、数字与连字符' },
  ],
}

function handleMore(command) {
  if (command === 'export') handleExport()
  if (command === 'clone') openClone()
}

async function handleExport() {
  const res = await exportCanvas(canvasId.value)
  // 后端吐的是 JSON 对象（引用型节点用自然键），这里落成文件；不走 blob 响应类型是
  // 为了让统一响应拦截器照常处理错误
  const blob = new Blob([JSON.stringify(res.data, null, 2)],
    { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `topo-${canvas.value.slug || canvasId.value}.json`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('已导出')
}

async function loadDicts() {
  if (dicts.projectGroups.length) return
  const [groups, channels, envs] = await Promise.all([
    fetchDictOptions(projectGroupApi),
    fetchDictOptions(channelApi),
    fetchDictOptions(environmentApi),
  ])
  dicts.projectGroups = groups.data?.results || []
  dicts.channels = channels.data?.results || []
  dicts.environments = envs.data?.results || []
}

async function openClone() {
  await loadDicts()
  Object.assign(cloneForm, {
    name: `${canvas.value.name} 副本`,
    slug: `${canvas.value.slug}-copy`,
    project_group: null, channel: null, environment: null,
    region: canvas.value.region || '',
  })
  cloneVisible.value = true
  cloneFormRef.value?.clearValidate()
}

async function submitClone() {
  await cloneFormRef.value.validate()
  cloning.value = true
  try {
    const body = {}
    Object.entries(cloneForm).forEach(([key, value]) => {
      if (value !== null && value !== '') body[key] = value
    })
    // region 要能显式清空成「跨地域」，所以空串也照传
    body.region = cloneForm.region
    const res = await cloneCanvas(canvasId.value, body)
    cloneVisible.value = false
    ElMessage.success(res.msg || '克隆完成')
    reportSkipped(res.data?.report)
    router.push({ name: 'TopoCanvasEditor', params: { id: res.data.canvas.id } })
  } finally {
    cloning.value = false
  }
}

/** 跳过的节点逐条列出来：只报个数量，人不知道要补哪些。 */
function reportSkipped(report) {
  const skipped = report?.skipped_nodes || []
  if (!skipped.length) return
  ElMessageBox.alert(
    skipped.map((s) => `${s.label}（${s.reason}）`).join('\n'),
    `有 ${skipped.length} 个节点没有落地`,
    { type: 'warning', customStyle: { whiteSpace: 'pre-line' } },
  )
}

function goBack() {
  router.push({ name: 'TopoCanvases' })
}

// 有未保存改动时离开页面给个拦截（浏览器刷新/关闭）
function beforeUnload(event) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

// 克隆后会 push 到新画布的同一个组件实例，onMounted 不会再跑一次——
// 不监听路由参数的话，页面标题换了、图却还是原来那张
watch(canvasId, (value, previous) => {
  if (value && previous && value !== previous) {
    dirty.value = false
    reload()
  }
})

onMounted(async () => {
  window.addEventListener('beforeunload', beforeUnload)
  window.addEventListener('keydown', onKeydown)
  const metaRes = await getTopoMeta()
  Object.assign(meta, metaRes.data || {})
  await reload()
  if (watchMode.value) startWatchTimer()
})
onUnmounted(() => {
  window.removeEventListener('beforeunload', beforeUnload)
  window.removeEventListener('keydown', onKeydown)
  stopWatchTimer()
})
</script>

<style scoped>
.topo-editor { display: flex; flex-direction: column; height: calc(100vh - 110px); }
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 12px; background: #fff; border: 1px solid #e4e7ed;
  border-radius: 4px; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;
}
.topbar-left, .topbar-right { display: flex; align-items: center; gap: 6px; }
.canvas-name { font-size: 15px; font-weight: 600; margin: 0 4px; }
.dirty { color: #e6a23c; font-size: 12px; }

.body { flex: 1; display: flex; gap: 8px; min-height: 0; }

/* 值班横幅 */
.watch-bar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 12px; margin-bottom: 8px; border-radius: 4px;
  font-size: 13px; border-left: 3px solid #67c23a; background: #f0f9eb; color: #529b2e;
}
.watch-bar.warning { border-left-color: #e6a23c; background: #fdf6ec; color: #b88230; }
.watch-bar.danger { border-left-color: #f56c6c; background: #fef0f0; color: #c45656; }
.watch-meta { color: #909399; font-size: 12px; }

.issues { display: flex; flex-direction: column; gap: 2px; }
.issue { font-size: 12px; line-height: 1.6; }
.issue.health-warning { color: #e6a23c; }
.issue.health-danger { color: #f56c6c; }

.palette {
  width: 168px; flex: none; overflow-y: auto; padding: 10px;
  background: #fff; border: 1px solid #e4e7ed; border-radius: 4px;
}
.palette-hint { font-size: 12px; color: #909399; line-height: 1.6; margin-bottom: 10px; }
.palette-group { margin-bottom: 12px; }
.palette-group-title {
  font-size: 12px; color: #606266; font-weight: 600; margin-bottom: 6px;
}
.palette-item { width: 100%; margin: 0 0 6px; justify-content: flex-start; }

.canvas-wrap {
  flex: 1; min-width: 0; position: relative;
  background: #fafbfc; border: 1px solid #e4e7ed; border-radius: 4px;
}
.empty-hint {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  max-width: 380px; text-align: center; color: #909399; font-size: 13px;
  line-height: 1.8; pointer-events: none;
}

.inspector {
  width: 260px; flex: none; overflow-y: auto; padding: 12px;
  background: #fff; border: 1px solid #e4e7ed; border-radius: 4px;
}
.inspector-title { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
.inspector-empty p { font-size: 12px; color: #606266; margin: 4px 0; }
.lost-warn { color: #f56c6c !important; }
.tip { font-size: 12px; color: #909399; line-height: 1.6; }
.mono { font-family: monospace; font-size: 12px; word-break: break-all; }
.ref-extra { margin: 0 0 12px; border-top: 1px dashed #e4e7ed; padding-top: 8px; }
.ref-extra-row { display: flex; gap: 8px; font-size: 12px; line-height: 1.9; }
.ref-extra-key { color: #909399; flex: none; width: 72px; }
.ref-extra-value { color: #303133; word-break: break-all; }

/* 节点卡片：左侧色条表健康度，与列表页 el-tag 配色同口径 */
.topo-node {
  width: 220px; padding: 8px 10px; background: #fff;
  border: 1px solid #dcdfe6; border-left: 4px solid #c0c4cc;
  border-radius: 4px; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
  font-size: 12px; text-align: left;
}
.topo-node.selected { border-color: #409eff; box-shadow: 0 0 0 2px rgba(64, 158, 255, 0.25); }
.topo-node.health-healthy { border-left-color: #67c23a; }
.topo-node.health-warning { border-left-color: #e6a23c; }
.topo-node.health-danger { border-left-color: #f56c6c; }
/* 值班模式下异常节点呼吸高亮：大屏离得远，只靠 4px 色条看不见 */
.topo-node.pulse { animation: node-pulse 1.6s ease-in-out infinite; }
@keyframes node-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(245, 108, 108, 0.5); }
  50% { box-shadow: 0 0 0 6px rgba(245, 108, 108, 0); }
}
.node-head { display: flex; justify-content: space-between; align-items: center; }
.node-kind { color: #409eff; font-weight: 600; cursor: text; }
.node-ref-flag { color: #909399; }
.node-label {
  font-size: 13px; color: #303133; margin-top: 2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.node-rename-kind {
  font-size: 12px;
  font-weight: 600;
  color: #409eff;
}
.node-rename {
  width: 100%;
  margin-top: 2px;
  padding: 1px 4px;
  font-size: 13px;
  font-family: inherit;
  color: #303133;
  border: 1px solid #409eff;
  border-radius: 2px;
  outline: none;
}
.node-services { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 4px; }
.svc-tag {
  padding: 0 4px;
  font-size: 11px;
  line-height: 16px;
  border-radius: 2px;
  white-space: nowrap;
}
/* 已部署 = 实心绿，规划中 = 虚线黄，与列表页 el-tag 配色同口径 */
.svc-tag.deployed { color: #529b2e; background: #f0f9eb; border: 1px solid #d1edc4; }
.svc-tag.planned {
  color: #b88230; background: #fdf6ec; border: 1px dashed #f3d19e;
}
.svc-tag.more { color: #606266; background: #f4f4f5; border: 1px solid #e9e9eb; cursor: default; }

.svc-list { display: flex; flex-direction: column; gap: 4px; }
.svc-row { display: flex; align-items: center; gap: 6px; }
.svc-meta { font-size: 12px; color: #909399; }

.node-sub, .node-status {
  color: #909399; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
</style>
