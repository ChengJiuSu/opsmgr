"""构建制品模块测试：CI 上报、签名下载、列表/删除与鉴权。"""
import hashlib
import os
import shutil
import tempfile
import time
from datetime import timedelta
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.asset_mgr.models import Channel, Environment, ProjectGroup
from apps.auth_mgr.apikey import ensure_service_user
from apps.auth_mgr.models import ApiCredential
from apps.system_cfg.models import SystemConfig

from .models import Artifact

User = get_user_model()

#: 已下线的全局 key，只用于「不该再被接受」的回归断言
LEGACY_KEY = 'test-artifact-key'

#: _report 的哨兵：区分「不传 api_key（用基类凭证）」与「显式传 None（不带头）」
_DEFAULT = object()
ARTIFACT_TMP = tempfile.mkdtemp(prefix='artifact_test_')


@override_settings(ARTIFACT_ROOT=ARTIFACT_TMP)
class ArtifactTestBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(ARTIFACT_TMP, ignore_errors=True)

    def setUp(self):
        # 上报走 API 凭证（旧的全局 artifact.api_key 已下线）。基类这把不限来源 IP，
        # 白名单与作用域的拦截由 ArtifactApiCredentialTests 单独覆盖
        raw, key_id, secret_hash = ApiCredential.generate_key()
        self.credential = ApiCredential.objects.create(
            name='at-base', key_id=key_id, secret_hash=secret_hash,
            scopes=['artifact:report', 'artifact:download'],
            ip_unrestricted=True, bound_user=ensure_service_user('at-base'))
        self.api_key = raw
        self.pg = ProjectGroup.objects.create(name='主项目', code='at-proj')
        self.ch = Channel.objects.create(name='官服', code='at-official')
        self.env = Environment.objects.create(name='测试环境', code='at-test')

    def _client(self, is_staff=True, username=None):
        username = username or ('admin_a' if is_staff else 'user_a')
        u = User.objects.create(username=username, is_staff=is_staff)
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def _report(self, content=b'package-bytes', version='1.0.0', api_key=_DEFAULT, **extra):
        data = {
            'file': SimpleUploadedFile(extra.pop('filename', 'app.tar.gz'), content),
            'project_group': self.pg.code, 'channel': self.ch.code, 'environment': self.env.code,
            'version': version,
        }
        data.update(extra)
        if api_key is _DEFAULT:
            api_key = self.api_key
        headers = {'HTTP_X_API_KEY': api_key} if api_key else {}
        return APIClient().post('/api/artifacts/report/', data, format='multipart', **headers)


class ArtifactReportTests(ArtifactTestBase):
    def test_report_success_writes_file_and_md5(self):
        content = b'hello-package'
        r = self._report(content=content, md5=hashlib.md5(content).hexdigest(),
                         jenkins_job='build-app', build_number=42)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['code'], 0)
        art = Artifact.objects.get()
        self.assertEqual(art.md5, hashlib.md5(content).hexdigest())
        self.assertEqual(art.size, len(content))
        self.assertEqual(art.jenkins_job, 'build-app')
        self.assertEqual(art.build_number, 42)
        self.assertEqual(art.file_path, os.path.join('at-proj', 'at-official', 'at-test', '1.0.0', 'app.tar.gz'))
        with open(art.abs_path(), 'rb') as f:
            self.assertEqual(f.read(), content)
        # 文件名进路径末段：命令行 curl -O / wget 直接落原名，无需额外参数
        self.assertIn(f'/api/artifacts/{art.id}/download/{art.original_name}?sig=',
                      r.json()['data']['download_url'])

    def test_report_md5_mismatch_removes_file(self):
        r = self._report(md5='0' * 32)
        self.assertEqual(r.status_code, 400)
        self.assertIn('md5 校验不一致', r.json()['msg'])
        self.assertFalse(Artifact.objects.exists())
        self.assertFalse(os.path.exists(
            os.path.join(ARTIFACT_TMP, 'at-proj', 'at-official', 'at-test', '1.0.0', 'app.tar.gz')))

    def test_report_duplicate_version_conflict(self):
        self._report()
        r = self._report(content=b'other')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Artifact.objects.count(), 1)

    def test_report_requires_api_key(self):
        # 401 而不是 403：接口挂了 ApiKeyAuthentication（带 authenticate_header），
        # 「没带/带错凭证」按未认证算
        self.assertEqual(self._report(api_key=None).status_code, 401)
        self.assertEqual(self._report(api_key='wrong-key').status_code, 401)

    def test_legacy_global_key_no_longer_accepted(self):
        """回归测试：旧的全局 artifact.api_key 兜底已删除，不能被谁又加回来。

        那把 key 全脚本共用、无来源限制、无作用域，且同时兼着下载签名密钥
        ——轮换一次会顺带作废所有下载 URL。
        """
        SystemConfig.objects.create(key='artifact.api_key', value=LEGACY_KEY)
        self.assertEqual(self._report(api_key=LEGACY_KEY).status_code, 401)

    def test_disabled_credential_rejected(self):
        self.credential.enabled = False
        self.credential.save(update_fields=['enabled'])
        self.assertEqual(self._report().status_code, 401)

    def test_report_rejects_path_traversal_version(self):
        r = self._report(version='../evil')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Artifact.objects.exists())

    def test_report_rejects_bad_filename(self):
        r = self._report(filename='a b#().tar.gz')
        self.assertEqual(r.status_code, 400)

    def test_report_unknown_dim_code(self):
        r = self._report(project_group='no-such-code')
        self.assertEqual(r.status_code, 400)
        self.assertIn('项目组不存在', r.json()['msg'])

    def test_report_disabled_dim_rejected(self):
        self.ch.enabled = False
        self.ch.save()
        r = self._report()
        self.assertEqual(r.status_code, 400)
        self.assertIn('渠道已停用', r.json()['msg'])


class ArtifactDownloadTests(ArtifactTestBase):
    def _make_artifact(self, content=b'data', version='2.0.0'):
        self._report(content=content, version=version)
        return Artifact.objects.get(version=version)

    def test_download_with_valid_sig(self):
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/', {'sig': art.download_sig()})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content), b'data')

    def test_download_with_bad_sig(self):
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/', {'sig': 'f' * 64})
        self.assertEqual(r.status_code, 403)

    def test_download_authenticated_user_without_sig(self):
        art = self._make_artifact()
        r = self._client(is_staff=False).get(f'/api/artifacts/{art.id}/download/')
        self.assertEqual(r.status_code, 200)

    def test_download_missing_file(self):
        art = self._make_artifact()
        os.remove(art.abs_path())
        r = APIClient().get(f'/api/artifacts/{art.id}/download/', {'sig': art.download_sig()})
        self.assertEqual(r.status_code, 404)

    @override_settings(ARTIFACT_XACCEL_PREFIX='/_artifacts/')
    def test_download_xaccel(self):
        """开了前缀就只回 header，文件体交给 nginx——响应必须是空体。"""
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/', {'sig': art.download_sig()})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b'')
        self.assertEqual(r['X-Accel-Redirect'], f'/_artifacts/{quote(art.file_path)}')
        self.assertIn('attachment', r['Content-Disposition'])

    def test_download_default_filename_is_original_name(self):
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/', {'sig': art.download_sig()})
        self.assertIn(art.original_name, r['Content-Disposition'])

    def test_download_named_path(self):
        """URL 末段带名：curl -O 靠它命名，且不影响签名校验。"""
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/{art.original_name}',
                            {'sig': art.download_sig()})
        self.assertEqual(r.status_code, 200)
        self.assertIn(art.original_name, r['Content-Disposition'])

    def test_download_named_path_wins_over_query(self):
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/from-path.tgz',
                            {'sig': art.download_sig(), 'filename': 'from-query.tgz'})
        self.assertIn('from-path.tgz', r['Content-Disposition'])
        self.assertNotIn('from-query.tgz', r['Content-Disposition'])

    def test_download_filename_override(self):
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/',
                            {'sig': art.download_sig(), 'filename': 'app-1.2.3.tar.gz'})
        self.assertIn('app-1.2.3.tar.gz', r['Content-Disposition'])

    def test_download_filename_sanitized(self):
        """filename 不进签名，必须当不可信输入洗：路径、换行、空值都不能进响应头。"""
        art = self._make_artifact()
        for raw, expect in (
            ('../../etc/passwd', 'passwd'),
            ('a\r\nX-Injected: 1', 'aX-Injected 1'),
            ('///', art.original_name),
        ):
            r = APIClient().get(f'/api/artifacts/{art.id}/download/',
                                {'sig': art.download_sig(), 'filename': raw})
            disposition = r['Content-Disposition']
            self.assertNotIn('\n', disposition)
            self.assertIn(expect, disposition)

    @override_settings(ARTIFACT_XACCEL_PREFIX='/_artifacts/')
    def test_download_xaccel_filename_override(self):
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/',
                            {'sig': art.download_sig(), 'filename': 'pkg.tgz'})
        self.assertIn('pkg.tgz', r['Content-Disposition'])
        self.assertEqual(r['X-Accel-Redirect'], f'/_artifacts/{quote(art.file_path)}')

    @override_settings(ARTIFACT_XACCEL_PREFIX='/_artifacts/')
    def test_download_xaccel_still_checks_sig(self):
        """内部重定向绕不开鉴权：签名不对不能吐出 X-Accel-Redirect。"""
        art = self._make_artifact()
        r = APIClient().get(f'/api/artifacts/{art.id}/download/', {'sig': 'f' * 64})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('X-Accel-Redirect', r)


class ArtifactViewSetTests(ArtifactTestBase):
    def test_list_filters_and_requires_auth(self):
        self._report(version='1.0.0')
        self.assertEqual(APIClient().get('/api/artifacts/').status_code, 401)
        c = self._client(is_staff=False)
        r = c.get('/api/artifacts/', {'project_group': self.pg.code, 'keyword': '1.0'})
        self.assertEqual(len(r.json()['data']['results']), 1)
        r = c.get('/api/artifacts/', {'env': 'no-such-env'})
        self.assertEqual(r.json()['data']['total'], 0)

    def test_destroy_admin_only_and_removes_file(self):
        self._report()
        art = Artifact.objects.get()
        self.assertEqual(self._client(is_staff=False).delete(f'/api/artifacts/{art.id}/').status_code, 403)
        path = art.abs_path()
        self.assertEqual(self._client().delete(f'/api/artifacts/{art.id}/').status_code, 204)
        self.assertFalse(Artifact.objects.exists())
        self.assertFalse(os.path.exists(path))

    def test_create_not_allowed(self):
        # 制品只能由 CI 上报，ViewSet 未开放 POST
        r = self._client().post('/api/artifacts/', {})
        self.assertEqual(r.status_code, 405)


class ArtifactModelTests(ArtifactTestBase):
    def _create(self, version):
        return Artifact.objects.create(
            project_group=self.pg, channel=self.ch, environment=self.env, version=version,
            file_path=f'x/{version}/a.tar.gz', original_name='a.tar.gz', md5='a' * 32, size=1,
        )

    def test_latest_for_returns_newest(self):
        self._create('1.0.0')
        newest = self._create('1.1.0')
        self.assertEqual(Artifact.latest_for(self.pg.id, self.ch.id, self.env.id).id, newest.id)

    def test_latest_for_locked_version(self):
        target = self._create('1.0.0')
        self._create('1.1.0')
        self.assertEqual(
            Artifact.latest_for(self.pg.id, self.ch.id, self.env.id, version='1.0.0').id, target.id)
        self.assertIsNone(Artifact.latest_for(self.pg.id, self.ch.id, self.env.id, version='9.9.9'))

    def test_latest_for_other_dims_isolated(self):
        self._create('1.0.0')
        other_env = Environment.objects.create(name='生产', code='at-prod')
        self.assertIsNone(Artifact.latest_for(self.pg.id, self.ch.id, other_env.id))


@override_settings(ARTIFACT_ROOT=ARTIFACT_TMP, TRUSTED_PROXIES=['127.0.0.1'])
class ArtifactApiCredentialTests(ArtifactTestBase):
    """上报/下载的作用域、IP 白名单与数据范围都要真的拦住。"""

    ALLOWED_IP = '203.0.113.9'

    def setUp(self):
        super().setUp()
        cache.clear()
        self.credential, self.key = self._credential('ci-report', ['artifact:report'])

    def tearDown(self):
        cache.clear()

    def _credential(self, name, scopes):
        raw, key_id, secret_hash = ApiCredential.generate_key()
        credential = ApiCredential.objects.create(
            name=name, key_id=key_id, secret_hash=secret_hash, scopes=scopes,
            ip_whitelist=self.ALLOWED_IP, bound_user=ensure_service_user(name),
        )
        return credential, raw

    def _report_with(self, key, ip=None, version='2.0.0'):
        data = {
            'file': SimpleUploadedFile('app.tar.gz', b'package-bytes'),
            'project_group': self.pg.code, 'channel': self.ch.code,
            'environment': self.env.code, 'version': version,
        }
        return APIClient().post('/api/artifacts/report/', data, format='multipart',
                                HTTP_X_API_KEY=key, REMOTE_ADDR=ip or self.ALLOWED_IP)

    def test_report_with_credential(self):
        r = self._report_with(self.key)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(Artifact.objects.filter(version='2.0.0').exists())

    def test_report_denied_outside_whitelist(self):
        r = self._report_with(self.key, ip='198.51.100.7')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_ip_denied')

    def test_report_denied_without_scope(self):
        credential, key = self._credential('dl-only', ['artifact:download'])
        r = self._report_with(key)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_scope_denied')

    def test_download_with_credential_scope(self):
        self._report_with(self.key)
        artifact = Artifact.objects.get(version='2.0.0')
        _, key = self._credential('dl', ['artifact:download'])
        r = APIClient().get(f'/api/artifacts/{artifact.id}/download/',
                            HTTP_X_API_KEY=key, REMOTE_ADDR=self.ALLOWED_IP)
        self.assertEqual(r.status_code, 200)

    def test_download_denied_without_scope(self):
        self._report_with(self.key)
        artifact = Artifact.objects.get(version='2.0.0')
        r = APIClient().get(f'/api/artifacts/{artifact.id}/download/',
                            HTTP_X_API_KEY=self.key, REMOTE_ADDR=self.ALLOWED_IP)
        self.assertEqual(r.status_code, 403)
        self.assertIn('artifact:download', r.json()['msg'])

    def test_download_signature_path_still_works(self):
        """CD 主机的签名下载不受影响：不带 key 时仍按签名判。"""
        self._report_with(self.key)
        artifact = Artifact.objects.get(version='2.0.0')
        url = f'/api/artifacts/{artifact.id}/download/'
        self.assertEqual(APIClient().get(url).status_code, 403)
        self.assertEqual(
            APIClient().get(f'{url}?sig={artifact.download_sig()}').status_code, 200)


@override_settings(ARTIFACT_ROOT=ARTIFACT_TMP)
class CleanArtifactsCommandTests(ArtifactTestBase):
    """制品保留期清理：过期删除、保底版本与锁定版本必须留下、孤儿文件扫盘。"""

    def _artifact(self, version, days_ago=0, content=b'pkg'):
        """建一条制品记录并落一个真实文件（清理要验文件也删掉了）。"""
        rel = os.path.join(self.pg.code, self.ch.code, self.env.code, version, 'app.tar.gz')
        dest = os.path.join(ARTIFACT_TMP, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(content)
        art = Artifact.objects.create(
            project_group=self.pg, channel=self.ch, environment=self.env, version=version,
            file_path=rel, original_name='app.tar.gz', md5='0' * 32, size=len(content),
        )
        if days_ago:
            # reported_at 是 auto_now_add，只能绕过 save 直接改
            Artifact.objects.filter(pk=art.pk).update(
                reported_at=timezone.now() - timedelta(days=days_ago))
            art.refresh_from_db()
        return art

    def _run(self, **kwargs):
        call_command('clean_artifacts', **kwargs)

    def test_expired_removed_with_file(self):
        old = self._artifact('1.0.0', days_ago=200)
        path = old.abs_path()
        self._run(keep_latest=0)
        self.assertFalse(Artifact.objects.filter(pk=old.pk).exists())
        self.assertFalse(os.path.exists(path))

    def test_fresh_kept(self):
        fresh = self._artifact('2.0.0')
        self._run(keep_latest=0)
        self.assertTrue(Artifact.objects.filter(pk=fresh.pk).exists())

    def test_keep_latest_survives_expiry(self):
        """长期不发版的环境里所有制品都会超期，全清掉之后连回滚都做不到。"""
        older = self._artifact('1.0.0', days_ago=300)
        newer = self._artifact('1.1.0', days_ago=200)
        self._run(keep_latest=1)
        self.assertFalse(Artifact.objects.filter(pk=older.pk).exists())
        self.assertTrue(Artifact.objects.filter(pk=newer.pk).exists())

    def test_locked_version_survives(self):
        """被任务模块锁定的版本清掉后，下次执行会直接失败。"""
        from apps.asset_mgr.models import ServiceType
        from apps.task_exec.models import ExecutionTask, TaskStep

        locked = self._artifact('1.0.0', days_ago=300)
        self._artifact('1.1.0', days_ago=300)
        service = ServiceType.objects.create(name='基础', code='ca-base')
        task = ExecutionTask.objects.create(
            name='CD', project_group=self.pg, channel=self.ch, environment=self.env)
        TaskStep.objects.create(
            task=task, order=1, name='部署', task_type='command', command='echo hi',
            service_type=service, params={'artifact_version': '1.0.0'})

        self._run(keep_latest=0)
        self.assertTrue(Artifact.objects.filter(pk=locked.pk).exists())
        self.assertFalse(Artifact.objects.filter(version='1.1.0').exists())

    def test_dry_run_deletes_nothing(self):
        old = self._artifact('1.0.0', days_ago=200)
        path = old.abs_path()
        self._run(keep_latest=0, dry_run=True)
        self.assertTrue(Artifact.objects.filter(pk=old.pk).exists())
        self.assertTrue(os.path.exists(path))

    def test_orphan_file_cleaned_when_expired(self):
        """记录已删、文件还在：兜住历史上删除失败的残留。"""
        orphan_dir = os.path.join(ARTIFACT_TMP, self.pg.code, self.ch.code, self.env.code, 'orphan')
        os.makedirs(orphan_dir, exist_ok=True)
        orphan = os.path.join(orphan_dir, 'left-behind.tar.gz')
        with open(orphan, 'wb') as f:
            f.write(b'junk')
        old_mtime = time.time() - 200 * 86400
        os.utime(orphan, (old_mtime, old_mtime))

        self._run()
        self.assertFalse(os.path.exists(orphan))

    def test_orphan_file_kept_when_fresh(self):
        """刚上报、记录还在事务里的文件不能被当孤儿删掉。"""
        fresh_dir = os.path.join(ARTIFACT_TMP, 'fresh-orphan')
        os.makedirs(fresh_dir, exist_ok=True)
        fresh = os.path.join(fresh_dir, 'new.tar.gz')
        with open(fresh, 'wb') as f:
            f.write(b'new')
        self._run()
        self.assertTrue(os.path.exists(fresh))

    def test_tracked_file_never_treated_as_orphan(self):
        art = self._artifact('3.0.0')
        old_mtime = time.time() - 300 * 86400
        os.utime(art.abs_path(), (old_mtime, old_mtime))
        self._run(keep_latest=0)  # 记录本身未过期，文件也不该被当孤儿
        self.assertTrue(os.path.exists(art.abs_path()))
