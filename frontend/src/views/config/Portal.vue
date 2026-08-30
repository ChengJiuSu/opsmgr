<template>
  <el-card shadow="never">
    <div class="portal-head">
      <div>
        <div class="portal-title">内部导航页</div>
        <div class="portal-hint">
          配置 <el-link type="primary" href="/nav.html" target="_blank">/nav.html</el-link>
          的内容与外观。导航列与内容区块相互独立：侧栏可以少列、换序或挂外链，不影响右侧内容。
        </div>
      </div>
      <el-button :icon="Refresh" @click="checkConfig">配置自检</el-button>
    </div>

    <el-tabs v-model="tab">
      <!-- ── 内容区块 ─────────────────────────────── -->
      <el-tab-pane label="内容区块" name="blocks">
        <div class="toolbar">
          <el-input
            v-model="blocks.keyword" placeholder="搜索标题/编码" clearable
            style="width:220px" @input="blocks.searchDebounced" @clear="blocks.search"
          />
          <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openBlock()">新增区块</el-button>
        </div>
        <el-table v-loading="blocks.loading" :data="blocks.rows" stripe>
          <el-table-column prop="title" label="区块标题" min-width="130" />
          <el-table-column prop="code" label="编码" min-width="110">
            <template #default="{ row }"><el-tag size="small" type="info">{{ row.code }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="tag" label="归类标签" min-width="110">
            <template #default="{ row }">{{ row.tag || '-' }}</template>
          </el-table-column>
          <el-table-column prop="system_count" label="系统数" width="80" />
          <el-table-column prop="sort_order" label="排序" width="70" />
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-switch v-if="canWrite" :model-value="row.enabled" @change="(v) => toggle(blockApi, blocks, row, v)" />
              <el-tag v-else size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="canWrite" label="操作" width="176" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :icon="Edit" @click="openBlock(row)">编辑</el-button>
              <el-button size="small" type="danger" :icon="Delete" @click="remove(blockApi, blocks, row, row.title, '区块下若仍有系统卡片或导航项指向它，将无法删除。')">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="blocks.page"
          :page-size="blocks.pageSize" :total="blocks.total"
          layout="total, prev, pager, next" class="pager"
          @current-change="blocks.fetch()"
        />
      </el-tab-pane>

      <!-- ── 系统清单 ─────────────────────────────── -->
      <el-tab-pane label="系统清单" name="systems">
        <div class="toolbar">
          <el-input
            v-model="systems.keyword" placeholder="搜索名称/缩写/域名" clearable
            style="width:220px" @input="systems.searchDebounced" @clear="systems.search"
          />
          <el-select v-model="systems.block" placeholder="所属区块" clearable style="width:160px" @change="systems.search">
            <el-option v-for="b in blockOptions" :key="b.id" :label="b.title" :value="b.id" />
          </el-select>
          <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openSystem()">新增系统</el-button>
        </div>
        <el-table v-loading="systems.loading" :data="systems.rows" stripe>
          <el-table-column prop="abbr" label="缩写" width="80">
            <template #default="{ row }"><span class="abbr">{{ row.abbr }}</span></template>
          </el-table-column>
          <el-table-column prop="name" label="系统名称" min-width="130" />
          <el-table-column prop="block_title" label="所属区块" min-width="110" />
          <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
          <el-table-column prop="domain" label="域名" min-width="160" show-overflow-tooltip />
          <el-table-column prop="sort_order" label="排序" width="70" />
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-switch v-if="canWrite" :model-value="row.enabled" @change="(v) => toggle(systemApi, systems, row, v)" />
              <el-tag v-else size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="canWrite" label="操作" width="176" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :icon="Edit" @click="openSystem(row)">编辑</el-button>
              <el-button size="small" type="danger" :icon="Delete" @click="remove(systemApi, systems, row, row.name)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="systems.page"
          :page-size="systems.pageSize" :total="systems.total"
          layout="total, prev, pager, next" class="pager"
          @current-change="systems.fetch()"
        />
      </el-tab-pane>

      <!-- ── 导航列 ───────────────────────────────── -->
      <el-tab-pane label="导航列" name="nav">
        <div class="toolbar">
          <span class="toolbar-note">侧栏按分组从上到下渲染；条目序号取自它指向的内容区块位置。</span>
          <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openGroup()">新增分组</el-button>
        </div>
        <el-table v-loading="navGroups.loading" :data="navGroups.rows" row-key="id" stripe>
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="nav-items">
                <div class="nav-items-head">
                  <span>「{{ row.name }}」下的导航项</span>
                  <el-button v-if="canCreate" size="small" :icon="Plus" @click="openItem(row)">新增导航项</el-button>
                </div>
                <el-table :data="row.items" size="small" stripe>
                  <el-table-column prop="label" label="显示名称" min-width="120">
                    <template #default="{ row: it }">{{ it.label || it.block_title }}</template>
                  </el-table-column>
                  <el-table-column label="指向" min-width="220">
                    <template #default="{ row: it }">
                      <el-tag v-if="it.block" size="small">区块 · {{ it.block_title }}</el-tag>
                      <el-tag v-else size="small" type="warning">外链 · {{ it.href }}</el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="sort_order" label="排序" width="70" />
                  <el-table-column label="启用" width="80">
                    <template #default="{ row: it }">
                      <el-tag size="small" :type="it.enabled ? 'success' : 'info'">{{ it.enabled ? '启用' : '停用' }}</el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column v-if="canWrite" label="操作" width="176">
                    <template #default="{ row: it }">
                      <el-button size="small" :icon="Edit" @click="openItem(row, it)">编辑</el-button>
                      <el-button size="small" type="danger" :icon="Delete" @click="remove(navItemApi, navGroups, it, it.label || it.block_title)">删除</el-button>
                    </template>
                  </el-table-column>
                </el-table>
                <el-empty v-if="!row.items.length" description="该分组下还没有导航项" :image-size="60" />
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="分组名称" min-width="140" />
          <el-table-column prop="en_name" label="英文名" min-width="120">
            <template #default="{ row }">{{ row.en_name || '-' }}</template>
          </el-table-column>
          <el-table-column label="导航项" width="90">
            <template #default="{ row }">{{ row.items.length }}</template>
          </el-table-column>
          <el-table-column prop="sort_order" label="排序" width="70" />
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-switch v-if="canWrite" :model-value="row.enabled" @change="(v) => toggle(navGroupApi, navGroups, row, v)" />
              <el-tag v-else size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="canWrite" label="操作" width="176" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :icon="Edit" @click="openGroup(row)">编辑</el-button>
              <el-button size="small" type="danger" :icon="Delete" @click="remove(navGroupApi, navGroups, row, row.name, '分组下的导航项会一并删除。')">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="navGroups.page"
          :page-size="navGroups.pageSize" :total="navGroups.total"
          layout="total, prev, pager, next" class="pager"
          @current-change="navGroups.fetch()"
        />
      </el-tab-pane>

      <!-- ── 维护公告 ─────────────────────────────── -->
      <el-tab-pane label="维护公告" name="notices">
        <div class="toolbar">
          <el-input
            v-model="notices.keyword" placeholder="搜索标题/编号" clearable
            style="width:220px" @input="notices.searchDebounced" @clear="notices.search"
          />
          <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openNotice()">新增公告</el-button>
        </div>
        <el-table v-loading="notices.loading" :data="notices.rows" stripe>
          <el-table-column prop="title" label="标题" min-width="160" />
          <el-table-column prop="code" label="编号" min-width="140">
            <template #default="{ row }"><el-tag size="small" type="info">{{ row.code }}</el-tag></template>
          </el-table-column>
          <el-table-column label="影响系统" min-width="180">
            <template #default="{ row }">
              <el-tag v-for="n in row.system_names" :key="n" size="small" class="chip">{{ n }}</el-tag>
              <span v-if="!row.system_names.length">-</span>
            </template>
          </el-table-column>
          <el-table-column label="时间窗口" min-width="230">
            <template #default="{ row }">{{ fmt(row.start_at) }} — {{ fmt(row.end_at) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.is_active ? 'danger' : 'info'">
                {{ row.is_active ? '生效中' : (row.enabled ? '未生效' : '停用') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="canWrite" label="操作" width="176" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :icon="Edit" @click="openNotice(row)">编辑</el-button>
              <el-button size="small" type="danger" :icon="Delete" @click="remove(noticeApi, notices, row, row.title)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="notices.page"
          :page-size="notices.pageSize" :total="notices.total"
          layout="total, prev, pager, next" class="pager"
          @current-change="notices.fetch()"
        />
      </el-tab-pane>

      <!-- ── 外观文案 ─────────────────────────────── -->
      <el-tab-pane label="外观文案" name="setting">
        <el-form :model="setting" label-width="130px" style="max-width:720px" :disabled="!canWrite">
          <el-divider content-position="left">品牌区</el-divider>
          <el-form-item label="方形标字符">
            <el-input v-model="setting.brand_mark" maxlength="4" show-word-limit style="width:140px" />
          </el-form-item>
          <el-form-item label="站点名称"><el-input v-model="setting.brand_name" /></el-form-item>
          <el-form-item label="英文副标"><el-input v-model="setting.brand_sub" /></el-form-item>

          <el-divider content-position="left">报头</el-divider>
          <el-form-item label="眉标"><el-input v-model="setting.masthead_eyebrow" /></el-form-item>
          <el-form-item label="主标题">
            <el-input v-model="setting.masthead_title" type="textarea" :rows="2" placeholder="每行一行标题" />
          </el-form-item>
          <el-form-item label="说明文字">
            <el-input v-model="setting.masthead_intro" type="textarea" :rows="3" />
          </el-form-item>
          <el-form-item label="统计标签">
            <el-input v-model="setting.masthead_systems_label" style="width:160px" />
            <el-input v-model="setting.masthead_groups_label" style="width:160px;margin-left:12px" />
          </el-form-item>

          <el-divider content-position="left">侧栏底部</el-divider>
          <el-form-item label="底部说明">
            <el-input
              v-model="setting.footer" type="textarea" :rows="3"
              placeholder="每行一条；用 **文字** 包裹的部分加粗"
            />
          </el-form-item>

          <el-divider content-position="left">布局</el-divider>
          <el-form-item label="侧栏宽度(px)">
            <el-input-number v-model="setting.sidebar_width" :min="200" :max="400" />
          </el-form-item>
          <el-form-item label="网格列数上限">
            <el-input-number v-model="setting.max_cols" :min="1" :max="6" />
          </el-form-item>
          <el-form-item label="最近访问条数">
            <el-input-number v-model="setting.recent_max" :min="0" :max="20" />
          </el-form-item>
          <el-form-item label="首次展开报头">
            <el-switch v-model="setting.masthead_open_by_default" />
          </el-form-item>

          <el-form-item v-if="canWrite">
            <el-button type="primary" :loading="savingSetting" @click="saveSetting">保存设置</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>
  </el-card>

  <!-- 区块弹窗 -->
  <el-dialog v-model="blockDlg" :title="(blockEdit ? '编辑' : '新增') + '内容区块'" width="480px">
    <el-form ref="blockFormRef" :model="blockForm" :rules="blockRules" label-width="90px">
      <el-form-item label="区块标题" prop="title"><el-input v-model="blockForm.title" /></el-form-item>
      <el-form-item label="编码" prop="code">
        <el-input v-model="blockForm.code" :disabled="!!blockEdit" placeholder="如 monitor" />
        <div class="tip">导航项与系统卡片据此关联，建后不建议再改</div>
      </el-form-item>
      <el-form-item label="归类标签"><el-input v-model="blockForm.tag" placeholder="显示在标题右侧的小号灰字" /></el-form-item>
      <el-form-item label="排序"><el-input-number v-model="blockForm.sort_order" :min="0" /></el-form-item>
      <el-form-item label="启用"><el-switch v-model="blockForm.enabled" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="blockDlg = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submitBlock">确定</el-button>
    </template>
  </el-dialog>

  <!-- 系统弹窗 -->
  <el-dialog v-model="systemDlg" :title="(systemEdit ? '编辑' : '新增') + '系统'" width="520px">
    <el-form ref="systemFormRef" :model="systemForm" :rules="systemRules" label-width="90px">
      <el-form-item label="所属区块" prop="block">
        <el-select v-model="systemForm.block" style="width:100%">
          <el-option v-for="b in blockOptions" :key="b.id" :label="b.title" :value="b.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="系统名称" prop="name"><el-input v-model="systemForm.name" /></el-form-item>
      <el-form-item label="缩写标" prop="abbr">
        <el-input v-model="systemForm.abbr" maxlength="6" show-word-limit style="width:160px" />
        <div class="tip">卡片左侧方标内的字符，建议 2-4 位</div>
      </el-form-item>
      <el-form-item label="描述">
        <el-input v-model="systemForm.description" type="textarea" :rows="2" maxlength="200" show-word-limit />
        <div class="tip">卡片上最多显示两行，超出截断</div>
      </el-form-item>
      <el-form-item label="域名" prop="domain">
        <el-input v-model="systemForm.domain" placeholder="monitor.corp.internal" />
        <div class="tip">页面按域名记住置顶与最近访问，必须唯一</div>
      </el-form-item>
      <el-form-item label="访问地址" prop="url"><el-input v-model="systemForm.url" placeholder="https://..." /></el-form-item>
      <el-form-item label="排序"><el-input-number v-model="systemForm.sort_order" :min="0" /></el-form-item>
      <el-form-item label="启用"><el-switch v-model="systemForm.enabled" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="systemDlg = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submitSystem">确定</el-button>
    </template>
  </el-dialog>

  <!-- 导航分组弹窗 -->
  <el-dialog v-model="groupDlg" :title="(groupEdit ? '编辑' : '新增') + '导航分组'" width="440px">
    <el-form ref="groupFormRef" :model="groupForm" :rules="groupRules" label-width="90px">
      <el-form-item label="分组名称" prop="name"><el-input v-model="groupForm.name" /></el-form-item>
      <el-form-item label="英文名"><el-input v-model="groupForm.en_name" placeholder="Operations" /></el-form-item>
      <el-form-item label="排序"><el-input-number v-model="groupForm.sort_order" :min="0" /></el-form-item>
      <el-form-item label="启用"><el-switch v-model="groupForm.enabled" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="groupDlg = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submitGroup">确定</el-button>
    </template>
  </el-dialog>

  <!-- 导航项弹窗 -->
  <el-dialog v-model="itemDlg" :title="(itemEdit ? '编辑' : '新增') + '导航项'" width="480px">
    <el-form ref="itemFormRef" :model="itemForm" :rules="itemRules" label-width="90px">
      <el-form-item label="所属分组"><el-input :model-value="itemGroupName" disabled /></el-form-item>
      <el-form-item label="指向类型">
        <el-radio-group v-model="itemForm.kind">
          <el-radio value="block">内容区块</el-radio>
          <el-radio value="href">外部链接</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="itemForm.kind === 'block'" label="内容区块" prop="block">
        <el-select v-model="itemForm.block" style="width:100%">
          <el-option v-for="b in blockOptions" :key="b.id" :label="b.title" :value="b.id" />
        </el-select>
      </el-form-item>
      <el-form-item v-else label="外链地址" prop="href">
        <el-input v-model="itemForm.href" placeholder="https://..." />
      </el-form-item>
      <el-form-item label="显示名称" prop="label">
        <el-input v-model="itemForm.label" :placeholder="itemForm.kind === 'block' ? '留空则取区块标题' : '外链必填'" />
      </el-form-item>
      <el-form-item label="排序"><el-input-number v-model="itemForm.sort_order" :min="0" /></el-form-item>
      <el-form-item label="启用"><el-switch v-model="itemForm.enabled" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="itemDlg = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submitItem">确定</el-button>
    </template>
  </el-dialog>

  <!-- 公告弹窗 -->
  <el-dialog v-model="noticeDlg" :title="(noticeEdit ? '编辑' : '新增') + '维护公告'" width="560px">
    <el-form ref="noticeFormRef" :model="noticeForm" :rules="noticeRules" label-width="90px">
      <el-form-item label="公告编号" prop="code">
        <el-input v-model="noticeForm.code" :disabled="!!noticeEdit" placeholder="如 2026-08-CHG-114" />
        <div class="tip">「不再提示本条」按编号记住，建后不要再改</div>
      </el-form-item>
      <el-form-item label="标题" prop="title"><el-input v-model="noticeForm.title" /></el-form-item>
      <el-form-item label="影响系统">
        <el-select v-model="noticeForm.systems" multiple filterable style="width:100%">
          <el-option v-for="s in systemOptions" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="时间窗口" prop="range">
        <el-date-picker
          v-model="noticeForm.range" type="datetimerange" style="width:100%"
          start-placeholder="开始" end-placeholder="结束" value-format="YYYY-MM-DDTHH:mm:ss"
        />
      </el-form-item>
      <el-form-item label="正文" prop="body">
        <el-input v-model="noticeForm.body" type="textarea" :rows="5" />
        <div class="tip">换行会原样保留</div>
      </el-form-item>
      <el-form-item label="启用"><el-switch v-model="noticeForm.enabled" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="noticeDlg = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submitNotice">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, h } from 'vue'
import { Plus, Edit, Delete, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { debounce } from '../../utils/debounce'
import {
  blockApi, systemApi, navGroupApi, navItemApi, noticeApi,
  getSetting, updateSetting, getPortalConfig, fetchOptions,
} from '../../api/portal'
import { useAuthStore } from '../../stores/auth'

const authStore = useAuthStore()
// 与其它基础字典同档：dict 有「可读可新增」这一档，新增与改删分开判
const canCreate = computed(() => authStore.can('dict', 'create'))
const canWrite = computed(() => authStore.can('dict', 'write'))

const tab = ref('blocks')
const submitting = ref(false)

// ── 列表状态工厂：五个 Tab 的分页/搜索/加载逻辑一致，抽出来避免五份重复 ──
function makeList(api, extraParams = () => ({})) {
  const state = reactive({
    rows: [], total: 0, page: 1, pageSize: 20,
    keyword: '', block: '', loading: false,
    async fetch() {
      state.loading = true
      try {
        const params = { page: state.page, page_size: state.pageSize, ...extraParams(state) }
        if (state.keyword) params.keyword = state.keyword
        const res = await api.list(params)
        state.rows = res.data?.results || []
        state.total = res.data?.total || 0
      } finally {
        state.loading = false
      }
    },
    search() {
      state.searchDebounced.cancel()
      state.page = 1
      state.fetch()
    },
    searchDebounced: null,
  })
  state.searchDebounced = debounce(state.search)
  return state
}

const blocks = makeList(blockApi)
const systems = makeList(systemApi, (s) => (s.block ? { block: s.block } : {}))
const navGroups = makeList(navGroupApi)
const notices = makeList(noticeApi)
const allLists = [blocks, systems, navGroups, notices]
onUnmounted(() => allLists.forEach((l) => l.searchDebounced.cancel()))

// 下拉选项
const blockOptions = ref([])
const systemOptions = ref([])
async function loadOptions() {
  const [b, s] = await Promise.all([fetchOptions(blockApi), fetchOptions(systemApi)])
  blockOptions.value = b.data?.results || []
  systemOptions.value = s.data?.results || []
}

// ── 通用启停与删除 ──
async function toggle(api, state, row, value) {
  try {
    await api.update(row.id, { ...row, enabled: value })
    row.enabled = value
  } catch {
    state.fetch()
  }
}

async function remove(api, state, row, name, extraHint = '') {
  await ElMessageBox.confirm(`确定删除「${name}」吗？${extraHint}`, '警告', { type: 'warning' })
  await api.remove(row.id)
  ElMessage.success('删除成功')
  state.fetch()
  loadOptions()
}

// ── 内容区块 ──
const blockDlg = ref(false)
const blockEdit = ref(null)
const blockFormRef = ref()
const blockForm = reactive({ title: '', code: '', tag: '', sort_order: 0, enabled: true })
const blockRules = {
  title: [{ required: true, message: '请输入区块标题' }],
  code: [
    { required: true, message: '请输入编码' },
    { pattern: /^[-a-zA-Z0-9_]+$/, message: '仅限字母、数字、中划线、下划线' },
  ],
}
function openBlock(row = null) {
  blockEdit.value = row
  Object.assign(blockForm, row ? { ...row } : { title: '', code: '', tag: '', sort_order: 0, enabled: true })
  blockDlg.value = true
  blockFormRef.value?.clearValidate()
}
async function submitBlock() {
  await blockFormRef.value.validate()
  submitting.value = true
  try {
    if (blockEdit.value) await blockApi.update(blockEdit.value.id, blockForm)
    else await blockApi.create(blockForm)
    ElMessage.success(blockEdit.value ? '更新成功' : '新增成功')
    blockDlg.value = false
    blocks.fetch()
    loadOptions()
  } finally {
    submitting.value = false
  }
}

// ── 系统清单 ──
const systemDlg = ref(false)
const systemEdit = ref(null)
const systemFormRef = ref()
const emptySystem = () => ({
  block: null, name: '', abbr: '', description: '', domain: '', url: '', sort_order: 0, enabled: true,
})
const systemForm = reactive(emptySystem())
const systemRules = {
  block: [{ required: true, message: '请选择所属区块' }],
  name: [{ required: true, message: '请输入系统名称' }],
  abbr: [{ required: true, message: '请输入缩写标' }],
  domain: [{ required: true, message: '请输入域名' }],
  url: [{ required: true, message: '请输入访问地址' }],
}
function openSystem(row = null) {
  systemEdit.value = row
  Object.assign(systemForm, row ? { ...row } : emptySystem())
  systemDlg.value = true
  systemFormRef.value?.clearValidate()
}
async function submitSystem() {
  await systemFormRef.value.validate()
  submitting.value = true
  try {
    if (systemEdit.value) await systemApi.update(systemEdit.value.id, systemForm)
    else await systemApi.create(systemForm)
    ElMessage.success(systemEdit.value ? '更新成功' : '新增成功')
    systemDlg.value = false
    systems.fetch()
    blocks.fetch()
    loadOptions()
  } finally {
    submitting.value = false
  }
}

// ── 导航分组 ──
const groupDlg = ref(false)
const groupEdit = ref(null)
const groupFormRef = ref()
const groupForm = reactive({ name: '', en_name: '', sort_order: 0, enabled: true })
const groupRules = { name: [{ required: true, message: '请输入分组名称' }] }
function openGroup(row = null) {
  groupEdit.value = row
  Object.assign(groupForm, row ? { ...row } : { name: '', en_name: '', sort_order: 0, enabled: true })
  groupDlg.value = true
  groupFormRef.value?.clearValidate()
}
async function submitGroup() {
  await groupFormRef.value.validate()
  submitting.value = true
  try {
    if (groupEdit.value) await navGroupApi.update(groupEdit.value.id, groupForm)
    else await navGroupApi.create(groupForm)
    ElMessage.success(groupEdit.value ? '更新成功' : '新增成功')
    groupDlg.value = false
    navGroups.fetch()
  } finally {
    submitting.value = false
  }
}

// ── 导航项 ──
const itemDlg = ref(false)
const itemEdit = ref(null)
const itemGroup = ref(null)
const itemFormRef = ref()
const emptyItem = () => ({ kind: 'block', label: '', block: null, href: '', sort_order: 0, enabled: true })
const itemForm = reactive(emptyItem())
const itemGroupName = computed(() => itemGroup.value?.name || '')
const itemRules = {
  block: [{ required: true, message: '请选择内容区块' }],
  href: [{ required: true, message: '请输入外链地址' }],
  label: [{
    validator: (_r, v, cb) =>
      (itemForm.kind === 'href' && !v ? cb(new Error('外链导航项必须填写显示名称')) : cb()),
  }],
}
function openItem(group, row = null) {
  itemGroup.value = group
  itemEdit.value = row
  Object.assign(itemForm, row
    ? { ...row, kind: row.block ? 'block' : 'href' }
    : emptyItem())
  itemDlg.value = true
  itemFormRef.value?.clearValidate()
}
async function submitItem() {
  await itemFormRef.value.validate()
  // 后端要求二选一：按当前选择清掉另一侧，避免把上次填的值一起提交过去
  const payload = {
    group: itemGroup.value.id,
    label: itemForm.label,
    sort_order: itemForm.sort_order,
    enabled: itemForm.enabled,
    block: itemForm.kind === 'block' ? itemForm.block : null,
    href: itemForm.kind === 'href' ? itemForm.href : '',
  }
  submitting.value = true
  try {
    if (itemEdit.value) await navItemApi.update(itemEdit.value.id, payload)
    else await navItemApi.create(payload)
    ElMessage.success(itemEdit.value ? '更新成功' : '新增成功')
    itemDlg.value = false
    navGroups.fetch()
  } finally {
    submitting.value = false
  }
}

// ── 公告 ──
const noticeDlg = ref(false)
const noticeEdit = ref(null)
const noticeFormRef = ref()
const emptyNotice = () => ({ code: '', title: '', systems: [], range: null, body: '', enabled: true })
const noticeForm = reactive(emptyNotice())
const noticeRules = {
  code: [{ required: true, message: '请输入公告编号' }],
  title: [{ required: true, message: '请输入标题' }],
  body: [{ required: true, message: '请输入正文' }],
  range: [{ required: true, message: '请选择时间窗口' }],
}
const fmt = (v) => (v ? String(v).replace('T', ' ').slice(0, 16) : '-')
function openNotice(row = null) {
  noticeEdit.value = row
  Object.assign(noticeForm, row
    ? { ...row, range: [row.start_at, row.end_at] }
    : emptyNotice())
  noticeDlg.value = true
  noticeFormRef.value?.clearValidate()
}
async function submitNotice() {
  await noticeFormRef.value.validate()
  const payload = {
    code: noticeForm.code,
    title: noticeForm.title,
    systems: noticeForm.systems,
    body: noticeForm.body,
    enabled: noticeForm.enabled,
    start_at: noticeForm.range[0],
    end_at: noticeForm.range[1],
  }
  submitting.value = true
  try {
    if (noticeEdit.value) await noticeApi.update(noticeEdit.value.id, payload)
    else await noticeApi.create(payload)
    ElMessage.success(noticeEdit.value ? '更新成功' : '新增成功')
    noticeDlg.value = false
    notices.fetch()
  } finally {
    submitting.value = false
  }
}

// ── 外观文案（单例） ──
const setting = reactive({})
const savingSetting = ref(false)
async function loadSetting() {
  const res = await getSetting()
  Object.assign(setting, res.data || {})
}
async function saveSetting() {
  savingSetting.value = true
  try {
    await updateSetting(setting)
    ElMessage.success('设置已保存')
  } finally {
    savingSetting.value = false
  }
}

// ── 配置自检：拉一次导航页真正会拿到的配置，把错配当场说清楚 ──
async function checkConfig() {
  const res = await getPortalConfig()
  const c = res.data || {}
  const warns = []
  const linked = new Set(c.nav.flatMap((g) => g.items.map((i) => i.block).filter(Boolean)))
  c.blocks.forEach((b) => {
    if (!c.systems.some((s) => s.block === b.id)) warns.push(`区块「${b.title}」下没有启用中的系统`)
    if (!linked.has(b.id)) warns.push(`区块「${b.title}」没有导航项指向它（页面照常显示）`)
  })
  if (!c.nav.length) warns.push('导航列为空，侧栏不会显示任何条目')

  ElMessageBox({
    title: '导航页配置自检',
    message: h('div', { style: 'line-height:1.7' }, [
      h('p', `当前下发：${c.blocks.length} 个区块 / ${c.systems.length} 个系统 / ${c.nav.length} 个导航分组 / ${c.notices.length} 条公告`),
      warns.length
        ? h('ul', { style: 'padding-left:18px;margin:8px 0 0' }, warns.map((w) => h('li', w)))
        : h('p', { style: 'color:var(--el-color-success)' }, '没有发现错配。'),
    ]),
    confirmButtonText: '知道了',
  })
}

onMounted(async () => {
  await loadOptions()
  allLists.forEach((l) => l.fetch())
  loadSetting()
})
</script>

<style scoped>
.portal-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; margin-bottom: 12px;
}
.portal-title { font-size: 16px; font-weight: 600; }
.portal-hint { margin-top: 4px; font-size: 13px; color: var(--el-text-color-secondary); }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.toolbar-note { font-size: 13px; color: var(--el-text-color-secondary); margin-right: auto; }
.toolbar > .el-button:last-child { margin-left: auto; }
.tip { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.5; }
.abbr {
  display: inline-block; padding: 1px 6px; font-size: 12px; font-weight: 600;
  border: 1px solid var(--el-border-color); color: var(--el-text-color-regular);
}
.chip { margin: 0 4px 4px 0; }
.nav-items { padding: 8px 16px 16px; }
.nav-items-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px; font-size: 13px; color: var(--el-text-color-secondary);
}
.pager { margin-top: 16px; justify-content: flex-end; }
</style>
