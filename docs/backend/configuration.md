# 配置说明

## 环境变量

所有敏感配置通过环境变量或 `.env` 文件注入（使用 `python-decouple`）。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SECRET_KEY` | `django-insecure-change-me-in-production` | Django 密钥，**生产必须修改** |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | 允许的主机名，逗号分隔 |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | 允许跨域的前端地址 |
| `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` | `60` | Access Token 有效期（分钟）|
| `JWT_REFRESH_TOKEN_LIFETIME_DAYS` | `7` | Refresh Token 有效期（天）|
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接地址 |

---

## 多环境配置

### 开发环境（dev）

默认激活，`manage.py` 中预设：

```python
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ops_manager.settings.dev')
```

| 项目 | 配置 |
|------|------|
| 数据库 | SQLite（`db.sqlite3`，零配置）|
| DEBUG | `True` |
| CORS | 允许所有源 |
| 日志 | 控制台输出，DEBUG 级别 |

### 生产环境（prod）

切换方式：

```bash
export DJANGO_SETTINGS_MODULE=ops_manager.settings.prod
```

生产环境需额外配置 MySQL 连接（`mysqlclient`）：

```bash
# 安装系统依赖（Ubuntu/Debian）
apt-get install -y default-libmysqlclient-dev pkg-config

# 安装 Python 包
pip install mysqlclient
```

---

## .env 文件示例

在项目根目录创建 `.env`（已添加到 `.gitignore`）：

```ini
SECRET_KEY=your-very-secret-key-here
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
CORS_ALLOWED_ORIGINS=https://yourdomain.com
JWT_ACCESS_TOKEN_LIFETIME_MINUTES=30
JWT_REFRESH_TOKEN_LIFETIME_DAYS=7
REDIS_URL=redis://127.0.0.1:6379/0
```

---

## Django 管理后台

访问地址：`http://localhost:8000/admin/`

创建超级用户：

```bash
python manage.py createsuperuser
# 或通过 shell
python manage.py shell -c "
from apps.auth_mgr.models import User
User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
"
```
