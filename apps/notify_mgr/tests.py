from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.im_mgr.models import FeishuApp, FeishuBinding, FeishuCardMessage, FeishuChannel
from common.testing import grant_sudo

from . import dispatch, events, webhook
from .models import NotifyDelivery, NotifyEvent, NotifyRule, WebhookRobot
from .sources import task_exec as source

PASSWORD = 'Pass!2345xyz'
TOKEN = '0f3a1c2e-7b4d-4e8f-9a6b-1c2d3e4f5a6b'
ALL_EVENTS = [source.EVENT_SUCCESS, source.EVENT_FAILED, source.EVENT_CLOSED]
FEISHU_APP = {
    'app_id': 'cli_test', 'app_secret': 'secret-value',
    'encrypt_key': 'encrypt-key-value', 'verification_token': 'token-value',
    'ip_whitelist': '203.0.113.0/24',
}


def _texts(payload):
    """递归收集卡片里的所有文本。"""
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == 'content' and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_texts(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_texts(item))
    return found


@override_settings(TASK_EXEC_BACKEND='sync', SITE_BASE_URL='https://ops.example.com')
class NotifyBase(TestCase):
    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup
        from apps.task_exec.models import ExecutionTask, TaskExecutionLog

        cache.clear()
        app = FeishuApp.load()
        for key, value in FEISHU_APP.items():
            setattr(app, key, value)
        app.enabled = True
        app.save()
        self.pg = ProjectGroup.objects.create(name='主项目', code='nt-proj')
        self.other_pg = ProjectGroup.objects.create(name='别的项目', code='nt-other')
        self.ch = Channel.objects.create(name='官服', code='nt-ch')
        self.other_ch = Channel.objects.create(name='华为', code='nt-hw')
        self.env = Environment.objects.create(name='测试环境', code='nt-env')
        self.user = get_user_model().objects.create(username='ops_zhang')
        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=self.pg, channel=self.ch, environment=self.env)
        self.log = TaskExecutionLog.objects.create(
            task=self.task, status='failed', trigger_type='execute', triggered_by=self.user,
            output='fatal: 连接超时', finished_at=timezone.now(),
            steps_result=[{'order': 1, 'name': '重启', 'status': 'failed'}])
        self.chat = FeishuChannel.objects.create(chat_id='oc_ops', name='运维群')
        self.robot = WebhookRobot.objects.create(name='告警机器人', hook_token=TOKEN)

    def _rule(self, target='chat', events_=None, **kwargs):
        if target == 'chat':
            kwargs.update(target_type=NotifyRule.TARGET_FEISHU_CHAT, feishu_chat=self.chat)
        else:
            kwargs.update(target_type=NotifyRule.TARGET_FEISHU_WEBHOOK, webhook_robot=self.robot)
        return NotifyRule.objects.create(
            name=kwargs.pop('name', f'规则{NotifyRule.objects.count() + 1}'),
            events=events_ or list(ALL_EVENTS), **kwargs)

    def _signal(self, kind='status', **extra):
        """模拟执行链路的状态广播，返回（应用机器人发送 mock, 群机器人发送 mock）。"""
        with patch('apps.im_mgr.client.send_card') as chat_send, \
                patch('apps.notify_mgr.webhook._post', return_value={'code': 0}) as hook_send:
            source.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id,
                                        kind=kind, **extra)
        return chat_send, hook_send


# ══════════════════════════════════════════════════════════
# 任务执行 → 事件
# ══════════════════════════════════════════════════════════

class TaskExecEventTests(NotifyBase):
    def test_failure_notified(self):
        self._rule()
        chat_send, _ = self._signal()
        chat_send.assert_called_once()
        chat_id, card = chat_send.call_args[0][:2]
        self.assertEqual(chat_id, 'oc_ops')
        text = ''.join(_texts(card))
        self.assertIn('重启网关', text)
        self.assertIn('连接超时', text)      # 失败附日志尾段
        self.assertIn('失败模块', text)
        self.assertIn('/task/executions/', text)

    def test_success_notified(self):
        self._rule()
        self.log.status = 'success'
        self.log.save(update_fields=['status'])
        chat_send, _ = self._signal()
        chat_send.assert_called_once()
        self.assertIn('执行成功', chat_send.call_args[0][1]['header']['title']['content'])

    def test_running_not_notified(self):
        self._rule()
        self.log.status = 'running'
        self.log.save(update_fields=['status'])
        self._signal()[0].assert_not_called()
        self.assertFalse(NotifyEvent.objects.exists())

    def test_steps_frames_ignored(self):
        """执行中的模块表刷新是高频帧，终态一定伴随一次状态推送。"""
        self._rule()
        self._signal(kind='steps')[0].assert_not_called()

    def test_task_switch_off_suppresses_everything(self):
        """测试任务关掉「消息通知」后，成功失败都不发。"""
        self._rule()
        self.task.notify_enabled = False
        self.task.save(update_fields=['notify_enabled'])
        self._signal()[0].assert_not_called()
        self.assertFalse(NotifyEvent.objects.exists())

    def test_repeated_broadcast_sends_once(self):
        """终态会被状态推送与模块表写入重复广播。"""
        self._rule()
        self._signal()
        chat_send, _ = self._signal()
        chat_send.assert_not_called()
        self.assertEqual(NotifyEvent.objects.count(), 1)

    def test_retry_failing_again_is_new_notification(self):
        """失败待处理重试后同一条执行记录再次失败，是一次新的失败。"""
        self._rule()
        self._signal()
        self.log.finished_at = timezone.now() + timedelta(minutes=5)
        self.log.save(update_fields=['finished_at'])
        self._signal()[0].assert_called_once()

    def test_retry_success_after_failure_notified(self):
        """失败通知发过之后重试成功，成功也必须发出去（旧播报会被去重吃掉）。"""
        self._rule()
        self._signal()
        self.log.status = 'success'
        self.log.save(update_fields=['status'])
        self._signal()[0].assert_called_once()

    def test_awaiting_failure_shows_deadline(self):
        self._rule()
        deadline = timezone.now() + timedelta(hours=1)
        self.task.pause_type = 'failed'
        self.task.resume_deadline = deadline
        self.task.save(update_fields=['pause_type', 'resume_deadline'])
        self.log.resume_state = {'names': '重启', 'remaining': 1}
        self.log.save(update_fields=['resume_state'])
        chat_send, _ = self._signal()
        text = ''.join(_texts(chat_send.call_args[0][1]))
        self.assertIn('失败待处理', text)
        self.assertIn(timezone.localtime(deadline).strftime('%H:%M'), text)

    def test_failure_closed_is_final_notification(self):
        """失败待处理终止 / 超时收尾，再发一次「最终失败」。"""
        self._rule()
        self._signal()
        chat_send, _ = self._signal(kind='failure_closed', reason='等待人工处理超时')
        chat_send.assert_called_once()
        card = chat_send.call_args[0][1]
        self.assertIn('最终失败', card['header']['title']['content'])
        self.assertIn('等待人工处理超时', ''.join(_texts(card)))

    def test_finish_failure_emits_closed_event(self):
        """与执行链路对接：trigger.finish_failure 发出 failure_closed。"""
        from apps.task_exec import trigger

        self._rule()
        self.log.resume_state = {'names': '重启', 'remaining': 0}
        self.log.save(update_fields=['resume_state'])
        with patch('apps.im_mgr.client.send_card'), \
                patch('apps.task_exec.trigger.push'), \
                patch('apps.task_exec.tasks.push'), \
                patch('apps.task_exec.live.push'):
            trigger.finish_failure(self.task, self.log, '人工确认终止')
        self.assertTrue(NotifyEvent.objects.filter(code=source.EVENT_CLOSED).exists())

    def test_rule_without_closed_event_skips_it(self):
        self._rule(events_=[source.EVENT_FAILED])
        self._signal(kind='failure_closed', reason='人工确认终止')[0].assert_not_called()

    def test_send_failure_does_not_escape(self):
        from apps.im_mgr.client import FeishuError

        self._rule()
        with patch('apps.im_mgr.client.send_card', side_effect=FeishuError('bot not in chat')):
            source.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id,
                                        kind='status')
        delivery = NotifyDelivery.objects.get()
        self.assertEqual(delivery.status, 'failed')
        self.assertIn('bot not in chat', delivery.error)
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'failed')

    def test_skips_chat_with_live_card(self):
        """从飞书发起的执行，那个会话里的卡片自己会刷成终态。"""
        self._rule()
        FeishuCardMessage.objects.create(log=self.log, chat_id='oc_ops', message_id='om_1')
        self._signal()[0].assert_not_called()
        self.assertEqual(NotifyDelivery.objects.get().status, 'skipped')

    def test_mention_trigger(self):
        FeishuBinding.objects.create(user=self.user, union_id='on_1', open_id='ou_zhang')
        self._rule(mention_trigger=True)
        chat_send, _ = self._signal()
        self.assertIn('<at id=ou_zhang></at>', ''.join(_texts(chat_send.call_args[0][1])))

    def test_no_mention_by_default(self):
        FeishuBinding.objects.create(user=self.user, union_id='on_1', open_id='ou_zhang')
        self._rule()
        chat_send, _ = self._signal()
        self.assertNotIn('<at', ''.join(_texts(chat_send.call_args[0][1])))


# ══════════════════════════════════════════════════════════
# 规则匹配与投递
# ══════════════════════════════════════════════════════════

class RuleMatchTests(NotifyBase):
    def test_blank_dimensions_mean_all(self):
        self._rule()
        self._signal()[0].assert_called_once()

    def test_project_group_filter_excludes(self):
        rule = self._rule()
        rule.project_groups.add(self.other_pg)
        self._signal()[0].assert_not_called()

    def test_channel_filter(self):
        rule = self._rule()
        rule.channels.add(self.other_ch)
        self._signal()[0].assert_not_called()
        rule.channels.add(self.ch)       # 多选里有一个命中即可
        self.log.finished_at = timezone.now() + timedelta(minutes=1)
        self.log.save(update_fields=['finished_at'])
        self._signal()[0].assert_called_once()

    def test_all_dimensions_match(self):
        rule = self._rule()
        rule.project_groups.add(self.pg)
        rule.channels.add(self.ch)
        rule.environments.add(self.env)
        self._signal()[0].assert_called_once()

    def test_disabled_rule_skipped(self):
        self._rule(enabled=False)
        self._signal()[0].assert_not_called()

    def test_disabled_target_recorded_as_skipped(self):
        self._rule()
        FeishuChannel.objects.filter(pk=self.chat.pk).update(enabled=False)
        self._signal()[0].assert_not_called()
        self.assertEqual(NotifyDelivery.objects.get().status, 'skipped')

    def test_two_rules_same_target_send_once(self):
        self._rule()
        self._rule()
        self._signal()[0].assert_called_once()

    def test_both_targets(self):
        """应用机器人与群机器人可以同时配，各发一次。"""
        self._rule('chat')
        self._rule('webhook')
        chat_send, hook_send = self._signal()
        chat_send.assert_called_once()
        hook_send.assert_called_once()
        self.assertEqual(
            set(NotifyDelivery.objects.values_list('status', flat=True)), {'success'})

    def test_feishu_app_not_ready(self):
        self._rule()
        FeishuApp.objects.update(enabled=False)
        self._signal()[0].assert_not_called()
        self.assertIn('飞书应用', NotifyDelivery.objects.get().error)

    def test_webhook_works_without_feishu_app(self):
        """只想要群机器人的部署，不需要配飞书应用。"""
        FeishuApp.objects.update(enabled=False)
        self._rule('webhook')
        _, hook_send = self._signal()
        hook_send.assert_called_once()

    def test_personal_open_id_target(self):
        self.chat.chat_id = 'ou_me'
        self.chat.save()
        self._rule()
        chat_send, _ = self._signal()
        self.assertEqual(chat_send.call_args[1]['receive_id_type'], 'open_id')

    def test_unregistered_event_ignored(self):
        self.assertIsNone(events.emit('nope.event', source_type='x', source_id=1,
                                      dedupe_key='x:1'))


# ══════════════════════════════════════════════════════════
# 群机器人客户端
# ══════════════════════════════════════════════════════════

class WebhookClientTests(NotifyBase):
    def test_normalize_token(self):
        self.assertEqual(webhook.normalize_token(TOKEN), TOKEN)
        self.assertEqual(webhook.normalize_token(
            f'https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}'), TOKEN)
        self.assertIsNone(webhook.normalize_token('https://evil.example.com/a'))
        self.assertIsNone(webhook.normalize_token('../../etc'))
        self.assertIsNone(webhook.normalize_token('abc'))

    def test_url_is_fixed_to_feishu(self):
        self.assertEqual(webhook.hook_url(TOKEN),
                         f'https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}')

    def test_sign_matches_feishu_algorithm(self):
        import base64
        import hashlib
        import hmac

        expected = base64.b64encode(hmac.new(
            b'1700000000\nsecret', b'', hashlib.sha256).digest()).decode()
        self.assertEqual(webhook.sign('1700000000', 'secret'), expected)

    def test_signed_payload(self):
        self.robot.secret = 'sec'
        self.robot.save()
        self.robot.refresh_from_db()
        with patch('apps.notify_mgr.webhook._post', return_value={'code': 0}) as post:
            webhook.send_card(self.robot, {'schema': '2.0'})
        payload = post.call_args[0][1]
        self.assertEqual(payload['msg_type'], 'interactive')
        self.assertEqual(payload['card'], {'schema': '2.0'})
        self.assertEqual(payload['sign'], webhook.sign(payload['timestamp'], 'sec'))

    def test_unsigned_payload(self):
        with patch('apps.notify_mgr.webhook._post', return_value={'code': 0}) as post:
            webhook.send_card(self.robot, {})
        self.assertNotIn('sign', post.call_args[0][1])

    def test_error_code_raises(self):
        with patch('apps.notify_mgr.webhook._post',
                   return_value={'code': 19021, 'msg': 'sign match fail'}):
            with self.assertRaisesMessage(webhook.WebhookError, 'sign match fail'):
                webhook.send_card(self.robot, {})

    def test_legacy_status_code_envelope(self):
        with patch('apps.notify_mgr.webhook._post',
                   return_value={'StatusCode': 19024, 'StatusMessage': 'Key Words Not Found'}):
            with self.assertRaisesMessage(webhook.WebhookError, 'Key Words Not Found'):
                webhook.send_card(self.robot, {})

    def test_rate_limit_retries_once(self):
        with patch('apps.notify_mgr.webhook._post',
                   side_effect=[{'code': 9499, 'msg': 'too many'}, {'code': 0}]) as post, \
                patch('apps.notify_mgr.webhook.time.sleep'):
            webhook.send_card(self.robot, {})
        self.assertEqual(post.call_count, 2)

    def test_token_encrypted_at_rest(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute('SELECT hook_token FROM notify_webhook_robot WHERE id=%s',
                           [self.robot.pk])
            raw = cursor.fetchone()[0]
        self.assertNotIn(TOKEN, raw)


# ══════════════════════════════════════════════════════════
# 配置面接口
# ══════════════════════════════════════════════════════════

class NotifyApiTests(NotifyBase):
    def _client(self, is_staff=True, sudo=True):
        username = 'admin_nt' if is_staff else 'user_nt'
        user = get_user_model().objects.create(username=username, is_staff=is_staff)
        user.set_password(PASSWORD)
        user.save()
        client = APIClient()
        resp = client.post('/api/auth/login/',
                           {'username': username, 'password': PASSWORD}, format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.json()['access'])
        if is_staff and sudo:
            grant_sudo(client)
        return client

    def test_non_admin_forbidden(self):
        client = self._client(is_staff=False)
        self.assertEqual(client.get('/api/notify/rules/').status_code, 403)
        self.assertEqual(client.get('/api/notify/robots/').status_code, 403)
        self.assertEqual(client.get('/api/notify/deliveries/').status_code, 403)

    def test_write_requires_sudo(self):
        client = self._client(sudo=False)
        self.assertEqual(client.get('/api/notify/robots/').status_code, 200)
        resp = client.post('/api/notify/robots/', {'name': 'x', 'hook_token': TOKEN},
                           format='json')
        self.assertEqual(resp.status_code, 403)

    def test_event_types(self):
        resp = self._client().get('/api/notify/events/')
        codes = [item['code'] for item in resp.json()['data']]
        self.assertEqual(codes[:3], ALL_EVENTS)

    def test_create_robot_from_full_url(self):
        resp = self._client().post('/api/notify/robots/', {
            'name': '值班群机器人',
            'hook_token': f'https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}',
            'secret': 'sec',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()['data'] if 'data' in resp.json() else resp.json()
        self.assertNotIn('hook_token', data)
        self.assertNotIn('secret', data)
        self.assertTrue(data['has_secret'])
        self.assertNotIn(TOKEN, str(data))
        robot = WebhookRobot.objects.get(name='值班群机器人')
        self.assertEqual(robot.hook_token, TOKEN)

    def test_create_robot_rejects_foreign_url(self):
        resp = self._client().post('/api/notify/robots/', {
            'name': 'x', 'hook_token': 'http://10.0.0.1/admin'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_create_robot_requires_token(self):
        resp = self._client().post('/api/notify/robots/', {'name': 'x'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_update_robot_keeps_secrets_when_blank(self):
        self.robot.secret = 'sec'
        self.robot.save()
        resp = self._client().patch(f'/api/notify/robots/{self.robot.pk}/',
                                    {'name': '改名', 'hook_token': '', 'secret': ''},
                                    format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.robot.refresh_from_db()
        self.assertEqual(self.robot.hook_token, TOKEN)
        self.assertEqual(self.robot.secret, 'sec')

    def test_clear_secret(self):
        self.robot.secret = 'sec'
        self.robot.save()
        self._client().patch(f'/api/notify/robots/{self.robot.pk}/',
                             {'clear_secret': True}, format='json')
        self.robot.refresh_from_db()
        self.assertEqual(self.robot.secret, '')

    def test_robot_test_endpoint(self):
        client = self._client()
        with patch('apps.notify_mgr.webhook._post', return_value={'code': 0}) as post:
            resp = client.post(f'/api/notify/robots/{self.robot.pk}/test/')
        self.assertEqual(resp.status_code, 200)
        post.assert_called_once()
        with patch('apps.notify_mgr.webhook._post',
                   return_value={'code': 19024, 'msg': 'Key Words Not Found'}):
            resp = client.post(f'/api/notify/robots/{self.robot.pk}/test/')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Key Words Not Found', resp.json()['msg'])

    def test_create_rule(self):
        resp = self._client().post('/api/notify/rules/', {
            'name': '主项目失败', 'events': [source.EVENT_FAILED, source.EVENT_FAILED],
            'target_type': 'feishu_webhook', 'webhook_robot': self.robot.pk,
            'project_groups': [self.pg.pk], 'channels': [self.ch.pk, self.other_ch.pk],
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        rule = NotifyRule.objects.get(name='主项目失败')
        self.assertEqual(rule.events, [source.EVENT_FAILED])
        self.assertEqual(rule.channels.count(), 2)

    def test_rule_validation(self):
        client = self._client()
        base = {'name': 'r', 'target_type': 'feishu_chat', 'feishu_chat': self.chat.pk}
        resp = client.post('/api/notify/rules/', {**base, 'events': []}, format='json')
        self.assertEqual(resp.status_code, 400)
        resp = client.post('/api/notify/rules/', {**base, 'events': ['x.y']}, format='json')
        self.assertEqual(resp.status_code, 400)
        resp = client.post('/api/notify/rules/', {
            'name': 'r', 'events': ALL_EVENTS, 'target_type': 'feishu_webhook'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_switch_target_clears_other(self):
        rule = self._rule('chat')
        resp = self._client().patch(f'/api/notify/rules/{rule.pk}/', {
            'target_type': 'feishu_webhook', 'webhook_robot': self.robot.pk}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        rule.refresh_from_db()
        self.assertIsNone(rule.feishu_chat_id)
        self.assertEqual(rule.webhook_robot_id, self.robot.pk)

    def test_rule_test_endpoint(self):
        rule = self._rule('chat')
        with patch('apps.im_mgr.client.send_card') as send:
            resp = self._client().post(f'/api/notify/rules/{rule.pk}/test/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(send.call_args[0][0], 'oc_ops')

    def test_deliveries_list(self):
        self._rule()
        self._signal()
        resp = self._client().get('/api/notify/deliveries/', {'status': 'success'})
        data = resp.json()['data']
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['results'][0]['event_label'], '任务执行失败')
        self.assertEqual(data['results'][0]['target_name'], '运维群')

    def test_deleting_target_removes_rules_keeps_records(self):
        rule = self._rule('webhook')
        self._signal()
        self.robot.delete()
        self.assertFalse(NotifyRule.objects.filter(pk=rule.pk).exists())
        delivery = NotifyDelivery.objects.get()
        self.assertIsNone(delivery.rule_id)
        self.assertEqual(delivery.target_name, '告警机器人')

    def test_task_notify_switch_in_api(self):
        resp = self._client().get(f'/api/exec/tasks/{self.task.pk}/')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body.get('data', body)['notify_enabled'])


class CleanRecordsTests(NotifyBase):
    def test_clean_old_records(self):
        from .tasks import clean_records

        old = NotifyEvent.objects.create(code=source.EVENT_FAILED, dedupe_key='old',
                                         source_type='x', source_id='1')
        NotifyEvent.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=60))
        NotifyEvent.objects.create(code=source.EVENT_FAILED, dedupe_key='new',
                                   source_type='x', source_id='2')
        self.assertEqual(clean_records(30), 1)
        self.assertEqual(NotifyEvent.objects.count(), 1)


class RenderTests(NotifyBase):
    def test_render_card_without_mentions(self):
        message = events.Message(title='t', elements=[], mention_open_ids=['ou_x'])
        self.assertNotIn('<at', str(dispatch.render_card(message, mention=False)))
        self.assertIn('<at id=ou_x></at>', str(dispatch.render_card(message, mention=True)))

    def test_deleted_log_builds_nothing(self):
        event = NotifyEvent.objects.create(code=source.EVENT_FAILED, dedupe_key='gone',
                                           source_type=source.SOURCE_TYPE, source_id='999999')
        self.assertIsNone(source.build(event))


# ══════════════════════════════════════════════════════════
# 角色权限：独立模块 notify（none / read / write）
# ══════════════════════════════════════════════════════════

class NotifyRolePermissionTests(NotifyBase):
    def _client_with(self, username, role=None, permissions=None, sudo=True):
        from apps.auth_mgr.models import Role

        if role is None:
            role = Role.objects.create(name=username, code=username,
                                       permissions=permissions or {})
        user = get_user_model().objects.create(username=username, role=role)
        user.set_password(PASSWORD)
        user.save()
        client = APIClient()
        resp = client.post('/api/auth/login/',
                           {'username': username, 'password': PASSWORD}, format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.json()['access'])
        if sudo:
            grant_sudo(client)
        return client

    def test_read_level_can_view_only(self):
        client = self._client_with('nt_reader', permissions={'notify': 'read'})
        for url in ('/api/notify/events/', '/api/notify/chats/', '/api/notify/rules/',
                    '/api/notify/robots/', '/api/notify/deliveries/'):
            self.assertEqual(client.get(url).status_code, 200, url)
        # 没有 read_create 档：新增按可选档向上取整到 write
        resp = client.post('/api/notify/robots/', {'name': 'x', 'hook_token': TOKEN},
                           format='json')
        self.assertEqual(resp.status_code, 403)
        with patch('apps.notify_mgr.webhook._post', return_value={'code': 0}) as post:
            resp = client.post(f'/api/notify/robots/{self.robot.pk}/test/')
        self.assertEqual(resp.status_code, 403)
        post.assert_not_called()

    def test_write_level_without_system(self):
        """配告警规则不需要系统配置档（那里含 LDAP / SMTP 口令）。"""
        client = self._client_with('nt_writer', permissions={'notify': 'write'})
        resp = client.post('/api/notify/robots/', {'name': '值班', 'hook_token': TOKEN},
                           format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        resp = client.post('/api/notify/rules/', {
            'name': 'r', 'events': ALL_EVENTS,
            'target_type': 'feishu_chat', 'feishu_chat': self.chat.pk}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        # 会话下拉走 notify 自己的接口；会话白名单管理仍是 system 档
        chats = client.get('/api/notify/chats/').json()['data']
        self.assertEqual(chats[0]['name'], '运维群')
        self.assertNotIn('chat_id', chats[0])
        self.assertEqual(client.get('/api/feishu/channels/').status_code, 403)

    def test_system_level_alone_no_longer_enough(self):
        client = self._client_with('nt_sys', permissions={'system': 'write', 'notify': 'none'})
        self.assertEqual(client.get('/api/notify/rules/').status_code, 403)

    def test_write_requires_sudo(self):
        client = self._client_with('nt_nosudo', permissions={'notify': 'write'}, sudo=False)
        resp = client.post('/api/notify/robots/', {'name': 'x', 'hook_token': TOKEN},
                           format='json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json().get('error_code'), 'sudo_required')

    def test_builtin_roles(self):
        from apps.auth_mgr.models import Role

        self.assertEqual(Role.objects.get(code='super').level_for('notify'), 'write')
        self.assertEqual(Role.objects.get(code='ops_admin').level_for('notify'), 'write')
        self.assertEqual(Role.objects.get(code='ops_member').level_for('notify'), 'none')
        client = self._client_with('nt_ops', role=Role.objects.get(code='ops_admin'))
        self.assertEqual(client.get('/api/notify/rules/').status_code, 200)

    def test_role_meta_lists_module(self):
        client = self._client()
        meta = client.get('/api/auth/roles/meta/').json()['data']
        module = next(m for m in meta['modules'] if m['value'] == 'notify')
        self.assertEqual([lv['value'] for lv in module['levels']], ['none', 'read', 'write'])
        group = next(g for g in meta['groups'] if g['label'] == '系统管理')
        self.assertIn('notify', group['modules'])

    def _client(self):
        return NotifyApiTests._client(self)


class GrantNotifyMigrationTests(TestCase):
    """auth_mgr 0011：存量角色补 notify 档，只补缺失键，自定义角色跟随 system。"""

    def _run(self):
        import importlib

        from django.apps import apps as django_apps

        mig = importlib.import_module('apps.auth_mgr.migrations.0011_grant_notify_module')
        mig.forwards(django_apps, None)

    def test_custom_roles_follow_system(self):
        from apps.auth_mgr.models import Role

        sys_admin = Role.objects.create(name='配置管理员', code='cfg',
                                        permissions={'system': 'write'})
        viewer = Role.objects.create(name='只读', code='viewer', permissions={'cmdb': 'read'})
        configured = Role.objects.create(name='已配置', code='done',
                                         permissions={'system': 'write', 'notify': 'read'})
        self._run()
        for role in (sys_admin, viewer, configured):
            role.refresh_from_db()
        self.assertEqual(sys_admin.permissions['notify'], 'write')
        self.assertEqual(viewer.permissions['notify'], 'none')
        self.assertEqual(configured.permissions['notify'], 'read')   # 不覆盖已配过的
