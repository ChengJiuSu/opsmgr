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
        <el-tooltip
          :content="flowDowngraded
            ? `连线超过 ${FLOW_MAX_EDGES} 条，已自动只让选中/悬停的链路流动`
            : '让全图连线都跑起来。默认只有鼠标指到的那条链路流动'"
          placement="bottom"
        >
          <!-- 降级时把开关一并禁掉：开着却不动比关着更让人困惑，tooltip 里说清为什么 -->
          <el-switch
            v-model="showFlow"
            active-text="流动"
            inline-prompt
            :disabled="flowDowngraded"
            style="margin-right:4px"
            @change="onShowFlowChange"
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
        <!-- 中段箭头的 marker 定义。放在画布外面单独一个零尺寸 <svg> 里：SVG 的 url(#id)
             是全文档解析的，不必塞进 vue-flow 自己那个 <svg>——塞进去反而会被它每次
             重渲染连坐。每种关系一个颜色，连线用 CSS 的 marker-mid 引用（见 TopoEdge） -->
        <svg class="marker-defs" aria-hidden="true" focusable="false">
          <defs>
            <marker
              v-for="m in ARROW_DEFS"
              :id="m.id"
              :key="m.id"
              :markerWidth="MID_ARROW_SIZE"
              :markerHeight="MID_ARROW_SIZE"
              :refX="MID_ARROW_SIZE / 2"
              :refY="MID_ARROW_SIZE / 2"
              orient="auto"
              markerUnits="userSpaceOnUse"
            >
              <path :d="arrowShape" :fill="m.color" />
            </marker>
          </defs>
        </svg>
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
          connection-mode="loose"
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
          @node-mouse-enter="onNodeMouseEnter"
          @node-mouse-leave="onNodeMouseLeave"
          @pane-mouse-enter="clearHover"
        >
          <Background :gap="16" pattern-color="#dfe4ec" />
          <Controls />
          <!-- 自定义连线：加宽交互描边 + 关系文字本身可点可右键（见 TopoEdge.vue） -->
          <template #edge-topo="edgeProps">
            <TopoEdge v-bind="edgeProps" />
          </template>
          <template #node-topo="{ id, data }">
            <div
              class="topo-node"
              :class="[
                `health-${data.health || 'none'}`,
                {
                  selected: selectedNodeId === id,
                  pulse: watchMode && data.health === 'danger',
                  // 链路之外的节点一并压暗，否则线暗了卡片还亮着，
                  // 视线仍旧被一片亮卡片拽走。悬停（transient）压得更狠并禁点击，
                  // 点击锁定时只压一半且保持可点——见 focus 的注释
                  dimmed: nodeDimmed(id),
                  transient: nodeDimmed(id) && !highlight.sticky,
                },
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
                >{{ serviceLabel(svc) }}</span>
                <el-tooltip
                  v-if="nodeServices(data).length > SERVICE_TAG_LIMIT"
                  placement="top"
                  :content="serviceSummary(nodeServices(data))"
                >
                  <span class="svc-tag more">+{{ nodeServices(data).length - SERVICE_TAG_LIMIT }}</span>
                </el-tooltip>
              </div>
              <!-- 四向连接点：竖着的关系（解析记录 → 证书）若只能右出左进，线会绕
                   一个大弯从卡片侧面兜过去，图上看着像连错了。connection-mode=loose
                   下每个点都能当起点也能当终点，所以一个方向只画一个 Handle -->
              <Handle
                v-for="pos in HANDLE_POSITIONS"
                :key="pos.id"
                :id="pos.id"
                type="source"
                :position="pos.position"
                class="topo-handle"
              />
            </div>
          </template>
        </VueFlow>
        <!-- 图例：颜色 + 线型是这张图的主要信息通道，不给对照表就等于让人猜。
             只列图上真实用到的关系，七种全摆出来反而要人先找哪几个跟自己有关 -->
        <div v-if="legendItems.length" class="edge-legend">
          <span v-for="item in legendItems" :key="item.value" class="legend-item">
            <i class="legend-line" :style="{ background: legendDash(item) }" />
            <i class="legend-arrow" :style="{ borderLeftColor: item.color }" />
            {{ item.label }}
          </span>
        </div>
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
            <el-form-item v-if="selectedNodeServices.length" label="服务进程">
              <div class="svc-list">
                <div v-for="svc in selectedNodeServices" :key="svc.code" class="svc-row">
                  <el-tag
                    size="small"
                    :type="svc.status === 'deployed' ? 'success' : 'warning'"
                    :effect="svc.status === 'deployed' ? 'light' : 'plain'"
                  >{{ svc.process_name || svc.name }}</el-tag>
                  <span class="svc-meta">
                    {{ svc.process_count }} 进程
                    <template v-if="svc.process_name">· {{ svc.name }}</template>
                    · {{ svc.status_label }}
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
import {
  ref, reactive, computed, watch, provide, onMounted, onUnmounted, nextTick,
} from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { VueFlow, Handle, Position, useVueFlow } from '@vue-flow/core'
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
import TopoEdge from './TopoEdge.vue'
import {
  relationStyle, computeFanOffsets, FLOW_MAX_EDGES, ARROW_DEFS, MID_ARROW_SIZE,
} from './edgeStyles'
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

//: 节点四周的连接点。id 直接用方位词，落库后一眼能读懂（TopoEdge.source_handle）
const HANDLE_POSITIONS = [
  { id: 'left', position: Position.Left },
  { id: 'right', position: Position.Right },
  { id: 'top', position: Position.Top },
  { id: 'bottom', position: Position.Bottom },
]
//: 没记连接点的老数据按「右出左进」画，与加连接点之前的走线一致
const DEFAULT_SOURCE_HANDLE = 'right'
const DEFAULT_TARGET_HANDLE = 'left'

//: 主机卡片上最多铺几个服务标签，多的收成 +n。一台机六个服务全铺开，卡片会比
//: 别的节点高一倍，整张图的行距就乱了
const SERVICE_TAG_LIMIT = 3
//: 「显示服务」是按人记的看图偏好，存 localStorage——与概览页区块显隐同口径，
//: 不进后端：它不影响任何人看到的数据，只影响这台浏览器上卡片有多高
const SHOW_SERVICES_KEY = 'topo.showServices'
//: 中段箭头的三角。留 1 的内边距，免得 marker 视窗边缘把斜边切出锯齿
const arrowShape = `M1,1 L${MID_ARROW_SIZE - 1},${MID_ARROW_SIZE / 2} L1,${MID_ARROW_SIZE - 1} Z`

/*
  流动动画的显隐按人记在本地：它不改变任何数据，只影响这台机器上图动不动。

  **默认关**。一开始是默认开的，理由是「进图第一眼就该看见流量往哪儿走」——实际用下来
  满图几十上百条线同时有东西在跑，是持续的动态噪声，看图的人反而定不下来看某一处。
  方向这件事交给箭头（端点一个 + 曲线正中一个）静态地说清楚就够了，动画留给「我正在
  追这一条链路」的时刻：鼠标指到哪个节点，那整条链路才亮起来并流动。
*/
const SHOW_FLOW_KEY = 'topo.showFlow'

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
const showFlow = ref(readLocalFlag(SHOW_FLOW_KEY, false))
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

/** 卡片上那一格：**进程名**优先（nginx-01×4）。

    进程名才是这台机上实际跑的那个东西的标识（「服务器分配」页自动编号，
    同云厂商+项目组+环境+渠道内按服务类型递增），排查时要 grep 的就是它；
    服务类型名（「战斗服」）是分类，同一类在不同机器上重名，写在卡片上等于
    没说这台机跑的是哪一个。没规划进程名的旧记录回落到类型名。

    口径与「服务器分配」页的「服务进程（规划/部署）」列一致：`名字 ×进程数`。 */
function serviceLabel(svc) {
  return `${svc.process_name || svc.name}×${svc.process_count}`
}

function serviceTitle(svc) {
  const parts = [serviceLabel(svc)]
  // 类型名放 hover：卡片要短，但「nginx-01 是干什么的」也得能问出来
  if (svc.process_name) parts.push(svc.name)
  parts.push(svc.status_label)
  return parts.join(' · ')
}

function serviceSummary(services) {
  return services.map(serviceTitle).join('\n')
}

const selectedNodeServices = computed(() => nodeServices(selectedNode.value?.data))

/** 本地看图偏好。没存过（或隐私模式下读不到）时用 fallback。读写都包 try：会直接抛。 */
function readLocalFlag(key, fallback = true) {
  try {
    const saved = localStorage.getItem(key)
    return saved === null ? fallback : saved === '1'
  } catch {
    return fallback
  }
}

function writeLocalFlag(key, value) {
  try {
    localStorage.setItem(key, value ? '1' : '0')
  } catch {
    // 存不下就只在本次会话生效，不值得为此报错
  }
}

function readShowServices() {
  return readLocalFlag(SHOW_SERVICES_KEY)
}

function onShowServicesChange(value) {
  writeLocalFlag(SHOW_SERVICES_KEY, value)
}

function onShowFlowChange(value) {
  writeLocalFlag(SHOW_FLOW_KEY, value)
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
  return buildEdge(`e${item.id}`, `n${item.source}`, `n${item.target}`, item.relation,
                   { source: item.source_handle, target: item.target_handle })
}

function buildEdge(id, source, target, relation, handles = {}) {
  return {
    id,
    source,
    target,
    // 自定义类型：默认连线的 1px 描边与不可点的标签层，右键根本点不到（见 TopoEdge.vue）
    type: 'topo',
    sourceHandle: handles.source || DEFAULT_SOURCE_HANDLE,
    targetHandle: handles.target || DEFAULT_TARGET_HANDLE,
    label: RELATION_LABELS.value[relation] || relation,
    // 箭头不在这里定：颜色要跟关系走，而关系可以在图上随时改（右键菜单/右侧下拉）。
    // 写死在边对象上就得在每个改关系的地方记着同步一次，漏一处就是「线紫箭头蓝」
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

/** 连线菜单：7 种关系**直接平铺**（当前值打勾），不做二级菜单。

    关系是这个菜单唯一的高频动作，藏进「改关系 ›」要多一次悬停对准；连线本身又细，
    鼠标从菜单项往子菜单挪的路上很容易划出去。7 行铺开一眼就点到。 */
function edgeMenuItems(edge) {
  if (!editable.value) return [{ key: 'noop', label: '值班模式下只读', disabled: true }]
  return [
    ...meta.relations.map((r) => ({
      key: `relation:${r.value}`,
      label: r.label,
      checked: edge.data.relation === r.value,
    })),
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
  openEdgeMenu(event, edge.id)
}

/** 连线菜单入口。两条路通到这里：右键连线本身（vue-flow 的 edgeContextMenu）、
    右键关系文字（TopoEdge 里的标签，那层收不到 vue-flow 的事件）。 */
function openEdgeMenu(event, edgeId) {
  const edge = edges.value.find((e) => e.id === edgeId)
  if (!edge) return
  selectedNodeId.value = ''
  selectedEdgeId.value = edgeId
  openMenu(event, { type: 'edge', id: edgeId }, edgeMenuItems(edge))
}

function selectEdge(edgeId) {
  selectedNodeId.value = ''
  selectedEdgeId.value = edgeId
}

provide('topoOpenEdgeMenu', openEdgeMenu)
provide('topoSelectEdge', selectEdge)

// ── 链路高亮 ──────────────────────────────────────────

/*
  点中或悬停一个节点 = 「这台机/这个域名，上下游是谁」。这是看拓扑图最高频的一问，
  原来只能靠眼睛顺着线爬，线一交叉就爬丢。

  实现上有两条路，这里刻意选了第二条：

  1. 改 edges 里每条边的 style/class —— 会触碰 v-model 绑定的数组，vue-flow 要走一遍
     边的 diff，而且很容易顺手把 dirty 标脏（看图不是改动，标脏了人会以为自己动了图）；
  2. 算一份集合 provide 下去，连线与节点自己读 —— 高亮只让若干个 computed 重算，
     边数组一个字节都不动。

  集合在这里算一次（O(V+E)），连线那边只做 has（O(1)）。反过来让每条边自己去
  遍历全图判断「我在不在链路上」，100 条边就是 100×100。
*/
const hoverNodeId = ref('')

//: 空集合复用同一个实例：没在悬停时不该每次重算都造两个新 Set
const EMPTY_SET = new Set()

/*
  高亮跟着谁走。两种来源，区别不只是「谁触发的」，还决定后面压暗压多狠：

  - 悬停（transient）：鼠标一挪就没，是「顺手瞥一眼」。压得狠一些没关系，
    而且期间可以把无关卡片的点击一并关掉——反正一秒后就恢复。
  - 点击（sticky）：选中一个节点后一直亮着，是「我要沿着这条链路看一会儿」。
    这种时候绝不能把其余卡片压到点不动：人接着要做的事十有八九就是点链路上的
    下一个节点，或者换一个节点看——把它们变成死图等于把人锁在当前选择里。

  悬停优先于选中：选中 A 之后把鼠标移到 B 上，人想看的是 B。松开手回到 A。
*/
const focus = computed(() => {
  if (hoverNodeId.value) return { id: hoverNodeId.value, sticky: false }
  if (selectedNodeId.value) return { id: selectedNodeId.value, sticky: true }
  return { id: '', sticky: false }
})

/*
  邻接索引。多跳遍历每跳都要问「从这个节点出去的边有哪些」，不建索引就是每跳扫一遍
  全部边，链路长一点当场退化成 O(V×E)。建一次是 O(E)，只依赖 edges——拖节点不重算。

  出边入边分开存：链路是有向的，顺流和逆流要分别走（见 highlight）。
*/
const adjacency = computed(() => {
  const outgoing = new Map()
  const incoming = new Map()
  for (const e of edges.value) {
    if (!outgoing.has(e.source)) outgoing.set(e.source, [])
    outgoing.get(e.source).push(e)
    if (!incoming.has(e.target)) incoming.set(e.target, [])
    incoming.get(e.target).push(e)
  }
  return { outgoing, incoming }
})

/**
 * 沿一个方向把链路走到底（BFS）。`index` 决定方向（出边 = 顺流 / 入边 = 逆流），
 * `nextOf` 从边上取该方向的另一端。命中的边与节点直接写进传进来的两个集合。
 *
 * 队列用游标而不是 shift()：shift 每次要挪整个数组，链路长的图上白白多一个 O(n²)。
 * seen 防环——拓扑图上「A 回源 B、B 又落地回 A」这种环是真实存在的，不防就死循环。
 */
function walkChain(startId, index, nextOf, nodeIds, edgeIds) {
  const queue = [startId]
  const seen = new Set([startId])
  for (let i = 0; i < queue.length; i += 1) {
    for (const e of index.get(queue[i]) || []) {
      edgeIds.add(e.id)
      const next = nextOf(e)
      nodeIds.add(next)
      if (seen.has(next)) continue
      seen.add(next)
      queue.push(next)
    }
  }
}

/*
  高亮整条链路，不是只有一跳。

  「这台机的上下游是谁」问的其实是一整条路径：域名 → 加速 → 负载均衡 → 后端机，
  只亮一跳等于让人点一下走一格，点到终点还得自己在脑子里把几段拼起来。

  方向上刻意分两次走，而不是把边当无向图 BFS：无向遍历会顺着任意一条边往回再往外爬，
  一路漫到整个连通分量——这张图上绝大多数节点是连通的，结果就是「全亮 = 没亮」。
  分开走则只亮「经过这个节点的那条链路」：所有能流到它的上游 + 它能流到的下游。
  共用同一个上游的旁支（兄弟节点）不亮——流量不经过它，它不在这条链路上。
*/
const highlight = computed(() => {
  const { id, sticky } = focus.value
  if (!id) return { active: false, sticky: false, nodes: EMPTY_SET, edges: EMPTY_SET }
  const { outgoing, incoming } = adjacency.value
  const nodeIds = new Set([id])
  const edgeIds = new Set()
  // 顺流：它能流到哪儿
  walkChain(id, outgoing, (e) => e.target, nodeIds, edgeIds)
  // 逆流：谁能流到它
  walkChain(id, incoming, (e) => e.source, nodeIds, edgeIds)
  return { active: true, sticky, nodes: nodeIds, edges: edgeIds }
})

//: 汇聚到同一连接点的多条线，末端各让开一点，否则箭头精确重合（见 edgeStyles.js）。
//: 只依赖 edges，拖节点不会重算
const fanOffsets = computed(() => computeFanOffsets(edges.value))

/*
  全图流动动画开不开。

  默认**关着**：满图都在动是噪声，方向由箭头静态交代（见 SHOW_FLOW_KEY 的注释）。
  这个开关是给「我就想看全图在流」的人留的口子。
  stroke-dashoffset 动不了合成器，每帧要重 paint 一遍路径——边一多就是整屏持续重绘，
  所以超过 FLOW_MAX_EDGES 自动降级为「只有选中/高亮的线在动」，不等人自己发现卡了再去
  翻开关。悬停时其余边被压暗，压暗的边一律停动画（见 TopoEdge.vue 的 flowing），
  所以真正在动的线数量只会比这个上限更少。
*/
const flowDowngraded = computed(() => edges.value.length > FLOW_MAX_EDGES)
const flowAll = computed(() => showFlow.value && !flowDowngraded.value)

provide('topoHighlight', highlight)
provide('topoFanOffsets', fanOffsets)
provide('topoFlowAll', flowAll)

function onNodeMouseEnter({ node }) {
  // 拖拽中不高亮：鼠标正压着一个节点走，沿途每个节点都会闪一下，图在抖
  if (dragSnapshot) return
  hoverNodeId.value = node.id
}

function onNodeMouseLeave() {
  hoverNodeId.value = ''
}

//: 鼠标从节点直接滑到画布空白（快速划过时 node-mouse-leave 偶尔会漏），
//: 在画布上兜一次底，免得整张图卡在压暗状态
function clearHover() {
  hoverNodeId.value = ''
}

/** 节点是否该压暗：有人在悬停、而它不在那条链路上。 */
function nodeDimmed(nodeId) {
  const state = highlight.value
  return state.active && !state.nodes.has(nodeId)
}

//: 图例：只列图上真实出现过的关系。七种全列出来，人得在一排色块里找哪个是自己图上有的
const legendItems = computed(() => {
  const used = new Set(edges.value.map((e) => e.data?.relation || 'custom'))
  return meta.relations
    .filter((r) => used.has(r.value))
    .map((r) => ({ ...r, ...relationStyle(r.value) }))
})

//: 图例里的线型要和画布上的完全一致，所以同样从配置读，不在 CSS 里另写一份
function legendDash(item) {
  return item.dash ? `repeating-linear-gradient(to right, ${item.color} 0 ${item.dash[0]}px, transparent ${item.dash[0]}px ${item.dash[0] + item.dash[1]}px)` : item.color
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
  // 去重看的是「哪两个节点 + 什么关系」，不看连接点：同一对节点之间同一种关系
  // 换个方向再连一条，图上就是两条重合的线，后端唯一约束也会拦
  const exists = edges.value.some(
    (e) => e.source === params.source && e.target === params.target
      && e.data.relation === relation)
  if (exists) {
    ElMessage.warning('这两个节点之间已有同类型连线，请改用其它关系')
    return
  }
  pushHistory()
  const id = `new-edge-${params.source}-${params.target}-${Date.now()}`
  edges.value.push(buildEdge(id, params.source, params.target, relation,
                             { source: params.sourceHandle, target: params.targetHandle }))
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
        source_handle: e.sourceHandle || '',
        target_handle: e.targetHandle || '',
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
/* ── 悬停联动与图例 ────────────────────────────────────── */

/* 链路之外的节点压暗。压的是整张卡片（含健康度色条与服务标签）——只压文字的话
   色条还亮着，远看仍是一片花的 */
/* 点击锁定时的压暗：压到「退到背景里」但仍然读得出、点得动。链路之外的节点
   下一秒很可能就是人要点的下一个目标 */
.topo-node.dimmed { opacity: 0.45; }
/* 悬停预览时的压暗：更狠，并且暂时不接受点击——鼠标此刻正贴着某个节点，
   误点旁边一张暗卡片会把选中态和右侧面板整个换掉。鼠标一挪就恢复 */
.topo-node.dimmed.transient {
  opacity: 0.15;
  pointer-events: none;
}
/* 只给透明度加过渡，不给 transform：节点同时还在被拖动，给 transform 加过渡会让
   拖拽出现橡皮筋一样的滞后 */
.topo-node { transition: opacity 0.15s ease; }

/* 只是放 <defs> 的容器，本身不该占位也不该吃事件 */
.marker-defs {
  position: absolute;
  width: 0;
  height: 0;
  pointer-events: none;
}

.edge-legend {
  position: absolute;
  left: 12px;
  bottom: 12px;
  z-index: 5;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  max-width: 60%;
  padding: 6px 10px;
  font-size: 11px;
  color: #606266;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  /* 图例是参照物不是操作对象，不能挡住底下的画布交互 */
  pointer-events: none;
  user-select: none;
}
.legend-item { display: inline-flex; align-items: center; gap: 2px; }
.legend-line { width: 20px; height: 2px; }
/* 纯 CSS 三角当箭头：为一排图例去内联七个 svg marker 不值当 */
.legend-arrow {
  width: 0;
  height: 0;
  margin-right: 4px;
  border-top: 3px solid transparent;
  border-bottom: 3px solid transparent;
  border-left: 5px solid currentColor;
}

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
/* 连接点：平时是个小灰点不抢视线，鼠标进到卡片上再放大变蓝，好瞄 */
.topo-handle {
  width: 8px;
  height: 8px;
  background: #c0c4cc;
  border: 1px solid #fff;
  transition: width 0.1s, height 0.1s, background 0.1s;
}
.topo-node:hover .topo-handle { width: 11px; height: 11px; background: #409eff; }

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
