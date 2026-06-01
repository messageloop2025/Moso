"""
数据库更新模块：按版本号依次执行升级脚本，将数据库从当前版本升级到最新版本。

**开发维护**：新增/修改本目录下迁移脚本后，若仓库使用 ``database/bundles/fresh_install.sql``
离线快照，请运行 ``python scripts/regenerate_fresh_install_sql.py`` 重新生成。

- 版本号：每次修改数据库结构，目标版本号 +1。当前数据库版本保存在 schema_version 表。
- 升级脚本：放在本目录下，命名格式 000_描述.py、001_描述.py、002_描述.py ...
  编号 N 表示「从版本 N 升级到版本 N+1」的脚本，仅负责这一档升级。
- 若数据库版本很老，会按顺序依次执行 000、001、002... 直到达到最新版本。
"""
import importlib.util
import logging
import re
from pathlib import Path

import aiosqlite

from database.schema_version import ensure_schema_version_table, get_schema_version, set_schema_version

logger = logging.getLogger("edgeops.database.migrations")

# 本目录
_MIGRATIONS_DIR = Path(__file__).resolve().parent

# 脚本命名：三位数字 + 下划线 + 描述 + .py，如 000_initial.py、001_add_xxx.py
_SCRIPT_PATTERN = re.compile(r"^(\d{3})_.+\.py$")


def _discover_scripts() -> list[tuple[int, Path]]:
    """扫描 migrations 目录，返回 (from_version, path) 列表，按 from_version 排序。"""
    out = []
    for p in _MIGRATIONS_DIR.iterdir():
        if not p.is_file() or p.name.startswith("_"):
            continue
        m = _SCRIPT_PATTERN.match(p.name)
        if m:
            from_ver = int(m.group(1), 10)
            out.append((from_ver, p))
    out.sort(key=lambda x: x[0])
    return out


def get_current_schema_version() -> int:
    """返回代码中定义的最新 schema 版本（有 N 个脚本则版本为 N，因 000 升到 1，001 升到 2…）。"""
    scripts = _discover_scripts()
    if not scripts:
        return 0
    # 脚本编号 000->1, 001->2,… 最新版本 = 最大编号 + 1
    return max(s[0] for s in scripts) + 1


async def _run_script(db: aiosqlite.Connection, from_version: int, path: Path) -> None:
    """加载并执行单个升级脚本；脚本需定义 async def upgrade(db)。"""
    spec = importlib.util.spec_from_file_location(f"migration_{from_version:03d}", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载迁移脚本: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "upgrade"):
        raise RuntimeError(f"迁移脚本缺少 upgrade(db) 函数: {path}")
    await mod.upgrade(db)


async def _is_original_database(db: aiosqlite.Connection) -> bool:
    """判断是否为引入版本号之前的旧库（已有真实用户数据）。

    仅当 ``users`` 表存在且**至少有一行**时才视为「无版本号的旧 毛竹 库」并跳过
    ``000_initial``：若只有空壳 ``users`` 表（常见于错误模板 / 部分建表脚本），
    **不得**跳过 000，否则将缺少预置管理员与 ``run_initial_schema`` 中的默认数据。
    """
    try:
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users' LIMIT 1"
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return False
        cur = await db.execute("SELECT 1 FROM users LIMIT 1")
        row = await cur.fetchone()
        await cur.close()
        return row is not None
    except Exception:
        return False


async def run_upgrades(db: aiosqlite.Connection) -> None:
    """
    检查当前数据库版本，并依次执行升级脚本直到最新版本。
    - 若当前版本为 0，先确保 schema_version 表存在。
    - 此前未使用版本号的旧库（已有 users 表且其中至少有一行用户）统一识别为原始数据库，直接标为版本 1，不执行 000。
    - 按 000、001、002... 顺序执行，每执行完一个将版本号 +1。
    """
    await ensure_schema_version_table(db)
    current = await get_schema_version(db)
    if current == 0 and await _is_original_database(db):
        logger.info("检测到原始数据库（无版本号时的旧库），统一标为版本 1，跳过 000 脚本")
        await set_schema_version(db, 1)
        current = 1
    target = get_current_schema_version()
    if current >= target:
        logger.info("数据库已为最新版本: %s (当前=%s, 目标=%s)", current, current, target)
        return
    scripts = _discover_scripts()
    for from_ver, path in scripts:
        if from_ver < current:
            continue
        if from_ver != current:
            raise RuntimeError(
                f"数据库版本 {current} 与脚本 {path.name} 不连续（脚本针对版本 {from_ver}），请检查迁移顺序"
            )
        logger.info("执行数据库升级: %s -> %s [%s]", from_ver, from_ver + 1, path.name)
        await _run_script(db, from_ver, path)
        await set_schema_version(db, from_ver + 1)
        current = from_ver + 1
    logger.info("数据库升级完成，当前版本: %s", current)
