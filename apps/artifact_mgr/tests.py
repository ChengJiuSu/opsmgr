"""构建制品模块测试：CI 上报、签名下载、列表/删除与鉴权。"""
import hashlib
import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.asset_mgr.models import Channel, Environment, ProjectGroup
from apps.auth_mgr.apikey import ensure_service_user
from apps.auth_mgr.models import ApiCredential
from apps.system_cfg.models import SystemConfig

from .models import Artifact

User = get_user_model()

API_KEY = 'test-artifact-key'
ARTIFACT_TMP = tempfile.mkdtemp(prefix='artifact_test_')


@override_settings(ARTIFACT_ROOT=ARTIFACT_TMP)
class ArtifactTestBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(ARTIFACT_TMP, ignore_errors=True)

    def setUp(self):
        SystemConfig.objects.create(key='artifact.api_key', value=API_KEY)
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

    def _report(self, content=b'package-bytes', version='1.0.0', api_key=API_KEY, **extra):
        data = {
            'file': SimpleUploadedFile(extra.pop('filename', 'app.tar.gz'), content),
            'project_group': self.pg.code, 'channel': self.ch.code, 'environment': self.env.code,
            'version': version,
        }
        data.update(extra)
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
        self.assertIn(f'/api/artifacts/{art.id}/download/?sig=', r.json()['data']['download_url'])

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
        # 401 而不是 403：接口现在挂了 ApiKeyAuthentication（带 authenticate_header），
        # 「没带/带错凭证」按未认证算，语义比原来的 403 更准
        self.assertEqual(self._report(api_key=None).status_code, 401)
        self.assertEqual(self._report(api_key='wrong-key').status_code, 401)

    def test_report_rejects_key_when_unconfigured(self):
        SystemConfig.objects.all().delete()
        os.environ.pop('ARTIFACT_API_KEY', None)
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
    """上报/下载走新的 API 凭证：作用域与 IP 白名单都要真的拦住。

    旧的全局 artifact.api_key 兜底路径由 ArtifactReportTests 覆盖，废弃期内两者并存。
    """

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
