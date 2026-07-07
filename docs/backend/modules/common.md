# 公共工具模块（common）

---

## 标准分页（`common/pagination.py`）

### 配置

```python
class StandardPagination(PageNumberPagination):
    page_size              = 20    # 默认每页
    page_size_query_param  = 'page_size'
    max_page_size          = 200   # 最大每页
```

### 统一分页响应格式

```json
{
  "code": 0,
  "data": {
    "total":     100,
    "page":      1,
    "page_size": 20,
    "results":   [ ... ]
  }
}
```

### 查询参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `page` | integer | 页码，从 1 开始 |
| `page_size` | integer | 每页条数，最大 200；**传 0 跳过分页一次性返回全量**（导出/同步场景），响应结构不变（`page=1`，`page_size=total`） |

---

## 统一响应 & 异常处理（`common/response.py`）

### 工具函数

```python
ok(data=None, msg='success')         # 成功响应，HTTP 200
fail(msg='error', code=1, status=400) # 失败响应
```

**示例：**

```python
from common.response import ok, fail

# ViewSet 中使用
return ok(data={'key': 'value'}, msg='创建成功')
return fail(msg='参数错误', code=1001, status=400)
```

### 全局异常处理器

在 `settings.py` 中注册：

```python
REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'common.response.custom_exception_handler',
}
```

**转换逻辑：**

DRF 默认错误响应格式多样（字符串/列表/嵌套字典），`custom_exception_handler` 将其统一为：

```json
{
  "code": 400,
  "msg": "该字段是必填项。",
  "data": null
}
```

**错误信息提取规则（`_extract_msg`）：**

| 输入类型 | 处理方式 |
|----------|----------|
| `str` | 直接返回 |
| `list` | 取第一个元素递归 |
| `dict` | 取第一个 value 递归 |
| 其他 | `str()` 转换 |
