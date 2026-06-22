"""每用户多组 AI 模型配置（Profile）与 active_profile_id。

从 schema 31 升级到 32。新建 ``user_ai_model_profiles`` 表、
``user_ai_config.active_profile_id`` 列，并将旧 ``user_ai_config`` 单行迁移为
首个「默认配置」Profile。幂等：重复执行安全。
"""


async def upgrade(db):
    from services.ai_model_profiles import ensure_profiles_schema, normalize_default_profile_names

    await ensure_profiles_schema(db)
    await normalize_default_profile_names(db)
