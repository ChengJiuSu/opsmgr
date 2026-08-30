import request from '../utils/request'

// 内部导航页（/nav.html）配置：区块 / 系统 / 导航分组 / 导航项 / 公告 + 全局设置
const createApi = (path) => ({
  list: (params) => request.get(`/api/portal/${path}/`, { params }),
  create: (data) => request.post(`/api/portal/${path}/`, data),
  update: (id, data) => request.put(`/api/portal/${path}/${id}/`, data),
  remove: (id) => request.delete(`/api/portal/${path}/${id}/`),
})

export const blockApi = createApi('blocks')
export const systemApi = createApi('systems')
export const navGroupApi = createApi('nav-groups')
export const navItemApi = createApi('nav-items')
export const noticeApi = createApi('notices')

// 全局设置是单例，没有 id
export const getSetting = () => request.get('/api/portal/setting/')
export const updateSetting = (data) => request.put('/api/portal/setting/', data)

// 导航页自身取数用的聚合接口，管理页只用它做「预览校验」
export const getPortalConfig = () => request.get('/api/portal/config/')

// 下拉选项：拉启用中的全量（页面规模是几十条，不做分页）
export const fetchOptions = (api) => api.list({ enabled: 'true', page_size: 200 })
