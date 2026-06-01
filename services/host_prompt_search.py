"""按主机级 AI 提示词内容搜索主机（REST / AI 工具共用）。"""
from __future__ import annotations

import re
from typing import Any

from api.auth import _is_admin_role
from api.hosts import _attach_user_tags_to_hosts, normalize_host_aliases_in_dict


async def search_hosts_by_prompt(
    db,
    user: dict,
    *,
    query: str,
    group_id: int | None = None,
    tag_ids: list[int] | None = None,
    regex: str = "",
    case_sensitive: bool = False,
    limit: int = 30,
    snippet_chars: int = 200,
) -> dict[str, Any]:
    """返回 { success, query, regex, group_id, tag_ids, count, hosts }。"""
    q_raw = (query or "").strip()
    if not q_raw:
        raise ValueError("query 不能为空")

    limit = min(100, max(1, int(limit)))
    snippet_chars = min(600, max(50, int(snippet_chars)))
    tag_ids = sorted(set(int(x) for x in (tag_ids or []) if x is not None))
    regex_pattern = (regex or "").strip()
    keywords = [kw.strip() for kw in q_raw.split() if kw.strip()] or [q_raw]
    keyword_or = " OR ".join(["LOWER(p.content) LIKE ?"] * len(keywords))
    keyword_params = [f"%{kw.lower()}%" for kw in keywords]

    sel_cols = """h.id, h.name, h.host, h.port, h.description, h.aliases, h.remark,
                          h.host_type, h.host_version, h.host_shell, h.host_package_manager,
                          p.content AS prompt_content, p.updated_at AS prompt_updated_at"""

    tag_filter_where = ""
    tag_filter_params: list = []
    if tag_ids:
        ph = ",".join(["?"] * len(tag_ids))
        tag_filter_where = (
            f"EXISTS (SELECT 1 FROM host_user_tags hutf "
            f"WHERE hutf.user_id = ? AND hutf.host_id = h.id AND hutf.tag_id IN ({ph}))"
        )
        tag_filter_params = [user["id"], *tag_ids]

    def _combine_where(*parts: str) -> str:
        return " AND ".join([p for p in parts if p])

    is_admin = _is_admin_role(user.get("role"))
    if group_id is not None:
        if is_admin:
            where_clause = _combine_where("m.group_id = ?", "p.user_id = ?", f"({keyword_or})", tag_filter_where)
            params = [group_id, user["id"], *keyword_params, *tag_filter_params]
            rows = await db.execute_fetchall(
                f"""SELECT {sel_cols}
                   FROM hosts h
                   INNER JOIN ai_host_prompts p ON p.host_id = h.id
                   INNER JOIN host_group_members m ON h.id = m.host_id
                   WHERE {where_clause}
                   ORDER BY h.name
                   LIMIT {limit}""",
                params,
            )
        else:
            where_clause = _combine_where(
                "m.group_id = ?",
                "p.user_id = ?",
                "(h.created_by = ? OR hs.id IS NOT NULL)",
                f"({keyword_or})",
                tag_filter_where,
            )
            params = [user["id"], user["id"], group_id, user["id"], user["id"], *keyword_params, *tag_filter_params]
            rows = await db.execute_fetchall(
                f"""SELECT {sel_cols}
                   FROM hosts h
                   INNER JOIN ai_host_prompts p ON p.host_id = h.id
                   INNER JOIN host_group_members m ON h.id = m.host_id
                   INNER JOIN host_groups hg ON hg.id = m.group_id AND hg.created_by = ?
                   LEFT JOIN host_shares hs
                     ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                   WHERE {where_clause}
                   ORDER BY h.name
                   LIMIT {limit}""",
                params,
            )
    else:
        if is_admin:
            where_clause = _combine_where("p.user_id = ?", f"({keyword_or})", tag_filter_where)
            params = [user["id"], *keyword_params, *tag_filter_params]
            rows = await db.execute_fetchall(
                f"""SELECT {sel_cols}
                   FROM hosts h
                   INNER JOIN ai_host_prompts p ON p.host_id = h.id
                   WHERE {where_clause}
                   ORDER BY h.name
                   LIMIT {limit}""",
                params,
            )
        else:
            where_clause = _combine_where(
                "p.user_id = ?",
                "(h.created_by = ? OR hs.id IS NOT NULL)",
                f"({keyword_or})",
                tag_filter_where,
            )
            params = [user["id"], user["id"], user["id"], *keyword_params, *tag_filter_params]
            rows = await db.execute_fetchall(
                f"""SELECT DISTINCT {sel_cols}
                   FROM hosts h
                   INNER JOIN ai_host_prompts p ON p.host_id = h.id
                   LEFT JOIN host_shares hs
                     ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                   WHERE {where_clause}
                   ORDER BY h.name
                   LIMIT {limit}""",
                params,
            )

    hosts: list[dict] = []
    for r in rows:
        d = normalize_host_aliases_in_dict(dict(r))
        prompt_text = d.pop("prompt_content", "") or ""
        d["prompt_updated_at"] = d.pop("prompt_updated_at", None)
        hay = prompt_text if case_sensitive else prompt_text.lower()
        idx = -1
        for kw in keywords:
            needle = kw if case_sensitive else kw.lower()
            i = hay.find(needle)
            if i >= 0 and (idx < 0 or i < idx):
                idx = i
        if idx < 0:
            continue
        half = snippet_chars // 2
        start = max(0, idx - half)
        end = min(len(prompt_text), idx + half)
        snippet = prompt_text[start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(prompt_text):
            snippet = snippet + "…"
        d["prompt_snippet"] = snippet
        d["prompt_length"] = len(prompt_text)
        hosts.append(d)

    await _attach_user_tags_to_hosts(db, hosts, int(user["id"]))

    if regex_pattern:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            reg = re.compile(regex_pattern, flags)
        except re.error as e:
            raise ValueError(f"regex 非法: {e}") from e
        filtered: list[dict] = []
        host_ids_keep = [h["id"] for h in hosts]
        if host_ids_keep:
            ph = ",".join(["?"] * len(host_ids_keep))
            content_rows = await db.execute_fetchall(
                f"SELECT host_id, content FROM ai_host_prompts WHERE user_id = ? AND host_id IN ({ph})",
                [user["id"], *host_ids_keep],
            )
            content_map = {int(cr["host_id"]): (cr["content"] or "") for cr in content_rows}
            for h in hosts:
                text = content_map.get(int(h["id"]), "")
                if reg.search(text):
                    filtered.append(h)
        hosts = filtered

    return {
        "success": True,
        "query": q_raw,
        "regex": regex_pattern or "",
        "group_id": group_id,
        "tag_ids": tag_ids,
        "count": len(hosts),
        "hosts": hosts,
    }
