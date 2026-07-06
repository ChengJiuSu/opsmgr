from django.apps import AppConfig


class AssetMgrConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.asset_mgr'
    label = 'asset_mgr'
    verbose_name = '资产管理'
