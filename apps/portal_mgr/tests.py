"""导航页配置模块测试：种子数据、CRUD 权限、校验规则、聚合取数口径。"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import (
    PortalBlock,
    PortalNavGroup,
    PortalNavItem,
    PortalNotice,
    PortalSetting,
    PortalSystem,
)
from .serializers import build_config

User = get_user_model()


class BaseApiTest(TestCase):
    def _client(self, is_staff=True, username=None):
        username = username or ('admin_p' if is_staff else 'user_p')
        u = User.objects.create(username=username, is_staff=is_staff)
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/',
                   {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c


class SeedTests(TestCase):
    def test_seed_data_created_by_migration(self):
        """迁移应内置整份占位配置，且设置为单例。"""
        self.assertEqual(PortalBlock.objects.count(), 9)
        self.assertEqual(PortalSystem.objects.count(), 31)
        self.assertEqual(PortalNavGroup.objects.count(), 4)
        self.assertEqual(PortalNavItem.objects.count(), 9)
        self.assertEqual(PortalNotice.objects.count(), 2)
        self.assertEqual(PortalSetting.objects.count(), 1)
        self.assertEqual(PortalSetting.load().brand_name, '内部系统导航台')

    def test_setting_is_singleton(self):
        """再存一条也只会落在 pk=1 上。"""
        PortalSetting(brand_name='另一份').save()
        self.assertEqual(PortalSetting.objects.count(), 1)
        self.assertEqual(PortalSetting.load().brand_name, '另一份')


class ConfigApiTests(BaseApiTest):
    def test_config_requires_login_but_not_dict_perm(self):
        """导航页是全员入口：普通用户能读整份配置，未登录不能。"""
        anon = APIClient()
        self.assertEqual(anon.get('/api/portal/config/').status_code, 401)

        normal = self._client(is_staff=False)
        r = normal.get('/api/portal/config/')
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        self.assertEqual(len(data['blocks']), 9)
        self.assertEqual(len(data['systems']), 31)
        self.assertEqual(len(data['nav']), 4)

    def test_config_shape_matches_page(self):
        """聚合结果的字段名必须与导航页渲染函数消费的形状一致。"""
        c = build_config()
        self.assertEqual(
            set(c.keys()), {'settings', 'nav', 'blocks', 'systems', 'notices'})
        self.assertEqual(
            set(c['systems'][0].keys()), {'block', 'name', 'abbr', 'desc', 'domain', 'url'})
        self.assertEqual(set(c['blocks'][0].keys()), {'id', 'title', 'tag'})
        self.assertEqual(
            set(c['notices'][0].keys()), {'id', 'title', 'systems', 'start', 'end', 'body'})
        # 多行文案拆成数组，加粗按 ** 分段
        self.assertEqual(c['settings']['masthead']['title'], ['公司内部系统', '统一入口'])
        self.assertEqual(c['settings']['footer'][0], ['维护方：', '基础架构部 · 运维平台组'])
        # 公告时间给成页面能解析的 'YYYY-MM-DD HH:MM'
        self.assertRegex(c['notices'][0]['start'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')

    def test_disabled_items_excluded(self):
        """停用的区块连同其下卡片一并不下发，指向它的导航项也要略过。"""
        block = PortalBlock.objects.get(code='monitor')
        block.enabled = False
        block.save()

        c = build_config()
        self.assertNotIn('monitor', [b['id'] for b in c['blocks']])
        self.assertNotIn('monitor', [s['block'] for s in c['systems']])
        ops = next(g for g in c['nav'] if g['group'] == '运维工具')
        self.assertNotIn('monitor', [i.get('block') for i in ops['items']])

    def test_empty_group_dropped(self):
        """分组下条目全被过滤掉时，分组本身不下发，避免侧栏留空壳。"""
        group = PortalNavGroup.objects.get(name='平台工具')
        group.items.update(enabled=False)
        self.assertNotIn('平台工具', [g['group'] for g in build_config()['nav']])


class CrudPermissionTests(BaseApiTest):
    def test_block_crud_and_permission(self):
        """管理接口按 dict 档：普通用户可查可增，不可改删。"""
        admin = self._client(is_staff=True)
        normal = self._client(is_staff=False)

        r = admin.post('/api/portal/blocks/',
                       {'code': 'sec-ops', 'title': '安全运营', 'tag': '运维工具'}, format='json')
        self.assertEqual(r.status_code, 201)
        bid = r.json()['id']

        self.assertEqual(normal.get('/api/portal/blocks/').status_code, 200)
        self.assertEqual(
            normal.patch(f'/api/portal/blocks/{bid}/', {'title': '改名'}, format='json').status_code,
            403)
        self.assertEqual(normal.delete(f'/api/portal/blocks/{bid}/').status_code, 403)
        self.assertEqual(admin.delete(f'/api/portal/blocks/{bid}/').status_code, 204)

    def test_block_delete_protected_when_referenced(self):
        """区块下还有卡片时禁止删除，报中文原因而不是 500。"""
        admin = self._client(is_staff=True)
        block = PortalBlock.objects.get(code='monitor')
        r = admin.delete(f'/api/portal/blocks/{block.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('无法删除', r.json()['msg'])

    def test_system_domain_unique(self):
        """域名是页面上的卡片标识，重复会让置顶/最近访问串档。"""
        admin = self._client(is_staff=True)
        block = PortalBlock.objects.get(code='monitor')
        r = admin.post('/api/portal/systems/', {
            'block': block.id, 'name': '重复域名', 'abbr': 'DUP',
            'domain': 'monitor.corp.internal', 'url': 'https://dup.corp.internal',
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_nav_item_requires_exactly_one_target(self):
        """导航项必须二选一：指向区块，或填外链。"""
        admin = self._client(is_staff=True)
        group = PortalNavGroup.objects.first()
        block = PortalBlock.objects.get(code='monitor')

        both = admin.post('/api/portal/nav-items/', {
            'group': group.id, 'label': '两个都填', 'block': block.id,
            'href': 'https://x.corp.internal'}, format='json')
        self.assertEqual(both.status_code, 400)

        neither = admin.post('/api/portal/nav-items/',
                             {'group': group.id, 'label': '都不填'}, format='json')
        self.assertEqual(neither.status_code, 400)

        link = admin.post('/api/portal/nav-items/', {
            'group': group.id, 'label': '集团制度库',
            'href': 'https://policy.corp.internal'}, format='json')
        self.assertEqual(link.status_code, 201)

    def test_notice_time_window_validated(self):
        """结束时间必须晚于开始时间。"""
        admin = self._client(is_staff=True)
        now = timezone.now()
        r = admin.post('/api/portal/notices/', {
            'code': 'BAD-001', 'title': '时间反了', 'body': '正文',
            'start_at': now.isoformat(), 'end_at': (now - timedelta(hours=1)).isoformat(),
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_setting_update_requires_write(self):
        """单例设置改的是全局展示口径，只给写档。"""
        admin = self._client(is_staff=True)
        normal = self._client(is_staff=False)

        self.assertEqual(normal.get('/api/portal/setting/').status_code, 200)
        self.assertEqual(
            normal.put('/api/portal/setting/', {'brand_name': '越权改名'}, format='json').status_code,
            403)

        r = admin.put('/api/portal/setting/', {'brand_name': '新导航台'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PortalSetting.load().brand_name, '新导航台')
