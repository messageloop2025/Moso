"""把 AI 成果物（artifacts）的存储根目录从 `charts/` 合并到 `chats/`。

背景：v020 里 artifacts 默认子目录是 `charts`，与 `chats`（聊天附件）仅差一个字母，
极易看错。本版本把两者统一放到 `chats/` 根下——附件仍是 `chats/YYYY/MM/DD/<uuid>.<ext>`
（文件），artifacts 仍是 `chats/YYYY/MM/DD/<slug>-<shortid>/`（目录），命名空间互不冲突。

升级动作（幂等）：
1. 扫描 `FS_DIR` 下每个用户目录；若存在 `charts/`，把它的每个 `YYYY/MM/DD/<leaf>` 子项
   搬到 `chats/YYYY/MM/DD/<leaf>`；
2. 搬移成功后删空 `charts/YYYY/MM/DD/`、`charts/YYYY/MM/`、`charts/YYYY/`，最后 `rmdir charts/`；
3. 不触碰 `ai_artifacts` 数据库行——它们只存相对 `storage_subdir`（如 `2026/04/22/abcd/`），
   与根目录无关，天然兼容新旧。
4. 搬移过程中的任何错误只记录 warning，不中断启动；本脚本可重复执行。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("edgeops.database.migrations.021")


def _merge_tree(src: Path, dst: Path) -> tuple[int, int]:
    """把 src 下的直接子项搬到 dst。返回 (moved, skipped)。

    - 如果 dst 下已存在同名子项：
      - 都是目录 → 递归下降继续合并；
      - 其它（dst 已有文件 / 类型不一致）→ 跳过，记 warning，以保留现有数据。
    """
    moved = 0
    skipped = 0
    dst.mkdir(parents=True, exist_ok=True)
    for child in list(src.iterdir()):
        target = dst / child.name
        try:
            if not target.exists():
                # 直接 rename（同盘下最快，跨盘会失败 → 回退到 copytree+rm）
                try:
                    child.rename(target)
                except OSError:
                    if child.is_dir():
                        shutil.copytree(child, target)
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        shutil.copy2(child, target)
                        try:
                            child.unlink()
                        except OSError:
                            pass
                moved += 1
            elif child.is_dir() and target.is_dir():
                # 递归合并目录
                m2, s2 = _merge_tree(child, target)
                moved += m2
                skipped += s2
                # 清掉空的源
                try:
                    child.rmdir()
                except OSError:
                    pass
            else:
                logger.warning(
                    "artifacts 目录迁移跳过（目标已存在且类型冲突）: %s -> %s",
                    child, target,
                )
                skipped += 1
        except OSError as exc:
            logger.warning("artifacts 目录迁移失败 %s -> %s: %s", child, target, exc)
            skipped += 1
    return moved, skipped


async def upgrade(db):  # noqa: D401 - 仅做磁盘侧合并，无 SQL 变更
    """只做文件系统侧的合并；无数据库变更。"""
    import config  # noqa: WPS433 - 延迟 import，避免对 api 层的循环依赖

    fs_dir = Path(getattr(config, "FS_DIR", Path(config.BASE_DIR) / "web" / "fs"))
    # 明确锁定"旧根名"为 'charts'，不读配置；如果配置被手工改回 'charts' 也视为旧数据。
    old_name = "charts"
    new_name = str(getattr(config, "ARTIFACT_SUBDIR", "chats") or "chats").strip().strip("/\\") or "chats"

    if new_name == old_name:
        logger.info("ARTIFACT_SUBDIR 仍为 '%s'，跳过 charts→chats 合并", old_name)
        return

    if not fs_dir.exists() or not fs_dir.is_dir():
        logger.info("FS_DIR 不存在，无需迁移: %s", fs_dir)
        return

    total_moved = 0
    total_skipped = 0
    touched_users = 0
    for user_dir in list(fs_dir.iterdir()):
        try:
            if not user_dir.is_dir():
                continue
        except OSError:
            continue
        old_root = user_dir / old_name
        if not old_root.exists() or not old_root.is_dir():
            continue
        new_root = user_dir / new_name
        try:
            m, s = _merge_tree(old_root, new_root)
            total_moved += m
            total_skipped += s
            # 尝试删空 old_root
            try:
                old_root.rmdir()
            except OSError:
                # 还有残留（目标已存在的冲突子项），保留原目录让人工排查
                logger.warning(
                    "旧 charts/ 目录未能完全清空，已保留: %s（冲突数=%d）",
                    old_root, s,
                )
            touched_users += 1
        except OSError as exc:
            logger.warning("处理用户目录失败 %s: %s", user_dir, exc)

    if touched_users:
        logger.info(
            "artifacts 目录合并完成：用户=%d，搬移=%d，跳过=%d（charts/ → %s/）",
            touched_users, total_moved, total_skipped, new_name,
        )
    else:
        logger.info("未发现需要合并的 charts/ 目录")
