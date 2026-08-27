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

---

## 请求元信息（`common/request.py`）

取客户端 IP 有**两个口径，不能混用**：

| 函数 | 用途 | 可信度 |
|------|------|--------|
| `client_ip(request)` | 审计展示、限速分桶 | 取 `X-Forwarded-For` 首段，**可被客户端伪造** |
| `auth_client_ip(request)` | 鉴权（API 凭证 IP 白名单） | 只信 `settings.TRUSTED_PROXIES` 之内的转发 |

`auth_client_ip` 的算法：

1. `REMOTE_ADDR` 不在 `TRUSTED_PROXIES` 内 → 请求没经过我们的代理，XFF 全是客户端
   自己写的，整条丢掉，直接用 `REMOTE_ADDR`；
2. 在可信代理内 → 从 XFF **最右往左**依次剥掉连续的可信代理，返回第一个非可信地址。
   右端是离服务最近的一跳（由代理追加，客户端伪造不到），左端才是客户端可控区；
3. 剥完没有非可信地址 → 返回 `REMOTE_ADDR`（代理自身），业务白名单必然不匹配，
   即 fail-closed。

为什么必须分两个函数：拿 `client_ip` 做白名单，攻击者加一个
`X-Forwarded-For: 10.0.0.5` 就伪装成内网来源，白名单等于没有；而审计侧要的恰恰是
「客户端声称的链路原样记录」，两者需求相反。

IP 规则匹配：`parse_ip_rules(text)`（逗号/空格/换行/分号混排 → 网段列表，非法条目抛
`ValueError` 供序列化器转中文报错）、`ip_in_networks(ip, networks)`、
`ip_matches_rules(ip, text)`（文本非法按不匹配处理）。

---

## 机读凭证口径（`common/secrets.py`）

`generate_secret(nbytes=32)` 生成 URL 安全随机明文（256 bit 熵）；`hash_secret(raw)`
返回 sha256 hex。Agent 令牌（`agent_mgr.models.hash_token` 是它的别名）与 API 凭证
共用这一份实现——口径散落两处时，改了其中一处会让另一处的存量凭证静默失效。

刻意不用 PBKDF2/bcrypt：凭证是随机串而非人选密码，不存在字典攻击面，而机读接口每个
请求都要校验一次，慢哈希会变成瓶颈。需要**可还原**的秘密（Webhook 签名密钥要用原值
重算 HMAC）走 `common/crypto.py` 的对称加密，不走这里。

---

## 可还原秘密的加密存储（`common/crypto.py`）

只存哈希做不到的场景——每次用都要拿原值——统一走 Fernet 对称加密：

| 使用者 | 为什么必须可还原 |
|--------|-----------------|
| MFA 的 TOTP 密钥 | 每次校验都要用原值重算验证码 |
| 执行变量的敏感值（`ExecVariable.is_secret`）| 要原样注入 ansible |
| 主机连接凭据的密码/私钥/提权密码 | 要下发给 ansible 建连 |
| 云账号 AccessKey Secret | 要用原值调厂商 API |
| API 凭证的 Webhook 签名密钥 | 要用原值重算 HMAC |

只存哈希的凭证（Agent 令牌、API 凭证 secret）走 `common/secrets.py`，不走这里。

**key 来源按序回退**：`SECRET_ENC_KEY` → `MFA_SECRET_KEY` → `SECRET_KEY`。
`MFA_SECRET_KEY` 是历史名字（那时唯一使用者是 MFA），保留以免存量部署换名后
密文全部解不开。

**换 key 会让已入库的密文全部解不开** —— 表现为 MFA 校验一律失败、任务执行报
变量解密失败、ansible 建连失败。生产必须显式配置并与 `SECRET_KEY` 分开轮换，
换 key 前先导出重新加密。

解不开时抛 `DecryptError`，调用方一律**报错中止**而不是回退空值：注入空密码去
连生产比中止危险得多。
