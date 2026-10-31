"""网页面板的 HTTP 接口。

前端在 views/关系面板/ 下，走 AstrBot 的插件视图桥（bridge-sdk.js），
后端就是这一层：全部是只读快照 + 少量写操作，写操作一律转交给
UserTagPlugin.write_relation / erase_relation，不另开一条改数据的路。

为什么要这样：主文件里那两个方法是唯一的落盘入口，撤销、备份、原子写、
数据迁移全挂在那条路上。面板绕过它自己改 self.data，就会出现「面板改了、
存档没改」或者「面板改了、撤销退不回去」这种最难查的 bug。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

# AstrBot 官方文档里，新插件推荐用 astrbot.api.web 读写请求/响应，
# 因为它和 Dashboard 内部框架解耦；Quart 只是「旧插件兼容」路径。
# 但 astrbot.api.web 是新版本才有的，而这个插件声明支持 >=4.0.0，
# 所以这里两套都接：有就用新的，没有就退回 Quart，行为一致。
try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.api.web import json_response as _json_response
    from astrbot.api.web import request as _request

    _WEB_API = True
except Exception:  # noqa: BLE001 - 老版本没有 astrbot.api.web
    from quart import jsonify as _json_response
    from quart import request as _request

    _WEB_API = False

from .relcore import (
    HIST_MAX,
    PHASE_STEPS,
    SOURCE_LABELS,
    category_range,
    phase_of,
    plugin_module,
    relation_category_of,
)
from .relexport import CONTRACT_STALE_SECONDS

PAGE_API_PREFIX = "/astrbot_plugin_user_tag/page"

USERS_DEFAULT_LIMIT = 24
USERS_MAX_LIMIT = 200


# ----------------------------------------------------------------------
# main.py 里有几个东西是**模块级**的，不在插件实例上：CATEGORY_BLURBS、
# names_of_category()、relation_category()。直接写 plugin.xxx 会 AttributeError，
# 而外面套了 try/except 就变成「静默空值」——真机上表现就是面板里所有人
# 都显示成「自定义」类别。所以统一从插件自己的模块里取，取不到再退回实例。
# ----------------------------------------------------------------------
def _category_blurbs(plugin: Any) -> List[Tuple[str, str]]:
    value = getattr(plugin_module(plugin), "CATEGORY_BLURBS", None)
    if value is None:
        value = getattr(plugin, "CATEGORY_BLURBS", None)
    return list(value or [])


def _names_of_category(plugin: Any, category: str) -> List[str]:
    fn = getattr(plugin_module(plugin), "names_of_category", None)
    if not callable(fn):
        fn = getattr(plugin, "names_of_category", None)
    if not callable(fn):
        return []
    try:
        return list(fn(category) or [])
    except Exception:  # noqa: BLE001
        return []


def _arg(name: str, default: Any = "") -> Any:
    """取 query 参数。astrbot.api.web 用 request.query，Quart 用 request.args。"""
    if _WEB_API:
        return _request.query.get(name, default)
    return _request.args.get(name, default)


def _header(name: str, default: str = "") -> str:
    try:
        return _request.headers.get(name, default)
    except Exception:
        return default


async def _body() -> Dict[str, Any]:
    """取 JSON 请求体。解析失败一律当空字典，不把解析错误抛到调用方。"""
    try:
        if _WEB_API:
            data = await _request.json(default={})
        else:
            data = await _request.get_json(silent=True)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _respond(payload: Dict[str, Any], code: int = 200):
    if _WEB_API:
        return _json_response(payload)
    if code == 200:
        return _json_response(payload)
    return _json_response(payload), code


def _ok(data: Any = None, **extra: Any):
    body: Dict[str, Any] = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return _respond(body)


def _err(message: str, code: int = 400):
    return _respond({"ok": False, "error": str(message), "code": code}, code)


def _text(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fmt_time(stamp: Any) -> str:
    try:
        stamp = int(stamp)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


class PanelApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._registered: List[str] = []
        self._unregistered = False

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register_routes(self) -> List[str]:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            self.plugin.log_warn(
                "当前 AstrBot 版本没有 register_web_api，面板接口未注册。"
            )
            return []
        specs: List[Tuple[str, Any, List[str], str]] = [
            ("/overview", self.overview, ["GET"], "UserTag panel overview"),
            ("/users", self.users, ["GET"], "UserTag panel user list"),
            ("/user", self.user_detail, ["GET"], "UserTag panel user detail"),
            ("/events", self.events, ["GET"], "UserTag panel change feed"),
            ("/meta", self.meta, ["GET"], "UserTag panel metadata"),
            ("/rules", self.rules, ["GET"], "UserTag panel evolution rules"),
            ("/templates", self.templates_get, ["GET"], "UserTag panel hint templates"),
            (
                "/templates/preview",
                self.templates_preview,
                ["POST"],
                "UserTag panel preview template",
            ),
            (
                "/templates/save",
                self.templates_save,
                ["POST"],
                "UserTag panel save template",
            ),
            ("/set", self.set_relation, ["POST"], "UserTag panel set relation"),
            ("/clear", self.clear_relation, ["POST"], "UserTag panel clear relation"),
            ("/note", self.set_note, ["POST"], "UserTag panel set note"),
            (
                "/group-set",
                self.set_group_relation,
                ["POST"],
                "UserTag panel set group relation",
            ),
            (
                "/export/status",
                self.export_status,
                ["GET"],
                "UserTag panel export status",
            ),
            (
                "/export/preview",
                self.export_preview,
                ["GET"],
                "UserTag panel export preview",
            ),
            ("/export/run", self.export_run, ["POST"], "UserTag panel export run"),
        ]
        for route, handler, methods, desc in specs:
            path = f"{PAGE_API_PREFIX}{route}"
            try:
                register(path, self._guard(handler), methods, desc)
                self._registered.append(path)
            except Exception as exc:  # pragma: no cover - 框架差异
                self.plugin.log_warn(f"面板路由 {path} 注册失败：{exc}")
        return list(self._registered)

    def unregister_routes(self) -> None:
        if self._unregistered:
            return
        self._unregistered = True
        context = getattr(self.plugin, "context", None)
        for path in list(self._registered):
            for name in ("unregister_web_api", "remove_web_api", "unregister_route"):
                remover = getattr(context, name, None)
                if not callable(remover):
                    continue
                try:
                    remover(path)
                except Exception:
                    pass
                break
        self._registered = []

    def _guard(self, handler: Any):
        """统一的入口闸门：面板关掉就 404，异常一律收成 JSON，不让 500 裸奔。"""

        async def guarded(*args: Any, **kwargs: Any):
            if not bool(self.plugin.config.get("panel_enabled", True)):
                return _err("面板已在插件配置里关闭（panel_enabled）。", 404)
            token = _text(self.plugin.config.get("panel_token", ""), 120)
            if token:
                given = _text(_header("X-Rel-Token", ""), 120)
                if given != token:
                    return _err("面板令牌不正确（panel_token）。", 403)
            try:
                return await handler(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - 兜底
                self.plugin.log_error(
                    f"面板接口 {getattr(handler, '__name__', '?')} 出错：{exc}"
                )
                return _err(f"内部错误：{exc}", 500)

        return guarded

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    async def overview(self):
        plugin = self.plugin
        core = plugin.core
        bots = plugin.bots()
        total = plugin.total_users()
        today = time.strftime("%Y-%m-%d", time.localtime())
        active_today = 0
        category_counter: Dict[str, int] = {}
        expiring = 0
        ttl_days = _int(plugin.config.get("relation_ttl_days", 0), 0)

        for bot in bots:
            for uid in plugin.data.get(bot, {}) or {}:
                if core.stat_of(bot, uid).get("days", {}).get(today):
                    active_today += 1
                relation = str(plugin.data.get(bot, {}).get(uid, "") or "")
                category = relation_category_of(plugin, relation)
                key = category or "自定义"
                category_counter[key] = category_counter.get(key, 0) + 1
                if ttl_days > 0 and core.expired(bot, uid, ttl_days):
                    expiring += 1

        recent = core.recent_events(12)
        return _ok(
            {
                "bots": len(bots),
                "total_users": total,
                "active_today": active_today,
                "expiring": expiring,
                "categories": [
                    {"name": name, "count": count}
                    for name, count in sorted(
                        category_counter.items(), key=lambda x: -x[1]
                    )
                ],
                "recent_events": [self._event_row(row) for row in recent],
                "export": plugin.exporter.status(),
                "generated_at": int(time.time()),
            }
        )

    async def users(self):
        plugin = self.plugin
        q = _text(_arg("q", ""), 80).lower()
        bot_filter = _text(_arg("bot", ""), 64)
        category_filter = _text(_arg("category", ""), 32)
        sort = _text(_arg("sort", "recent"), 16) or "recent"
        try:
            limit = max(
                1,
                min(
                    USERS_MAX_LIMIT,
                    _int(_arg("limit", USERS_DEFAULT_LIMIT), USERS_DEFAULT_LIMIT),
                ),
            )
        except (TypeError, ValueError):
            limit = USERS_DEFAULT_LIMIT
        page = max(1, _int(_arg("page", 1), 1))

        rows: List[Dict[str, Any]] = []
        for bot in plugin.bots():
            if bot_filter and bot != bot_filter:
                continue
            for uid in plugin.data.get(bot, {}) or {}:
                row = self._user_row(bot, uid)
                if category_filter and row["category"] != category_filter:
                    continue
                if q:
                    haystack = f"{row['uid']} {row['relation']} {row['note']} {row['nickname']}".lower()
                    if q not in haystack:
                        continue
                rows.append(row)

        if sort == "affection":
            rows.sort(key=lambda r: (-r["affection"], -r["last_at"]))
        elif sort == "name":
            rows.sort(key=lambda r: (r["relation"], r["uid"]))
        elif sort == "uid":
            rows.sort(key=lambda r: r["uid"])
        else:
            rows.sort(key=lambda r: -r["last_at"])

        total = len(rows)
        start = (page - 1) * limit
        window = rows[start : start + limit]
        return _ok(
            {
                "total": total,
                "page": page,
                "limit": limit,
                "pages": max(1, (total + limit - 1) // limit),
                "bots": plugin.bots(),
                "items": window,
            }
        )

    async def user_detail(self):
        plugin = self.plugin
        bot = _text(_arg("bot", ""), 64) or (
            plugin.bots()[0] if plugin.bots() else "default"
        )
        uid = _text(_arg("uid", ""), 64)
        if not uid:
            return _err("缺少 uid。")
        row = self._user_row(bot, uid)
        core = plugin.core
        hist = [
            {
                "at": item.get("at", 0),
                "at_text": _fmt_time(item.get("at")),
                "from": item.get("from", ""),
                "to": item.get("to", ""),
                "by": item.get("by", ""),
                "by_text": SOURCE_LABELS.get(item.get("by", ""), item.get("by", "")),
                "note": item.get("note", ""),
            }
            for item in reversed(core.hist_of(bot, uid))
        ]
        groups = []
        for umo, users in (core.group_rel.get(bot, {}) or {}).items():
            relation = users.get(uid)
            if relation:
                groups.append({"umo": umo, "relation": relation})
        row["history"] = hist[:HIST_MAX]
        row["groups"] = groups
        row["trend"] = [
            {"date": day, "count": count}
            for day, count in core.recent_counts(bot, uid, 14)
        ]
        row["hint_preview"] = self._hint_preview(row["relation"], row["note"])
        return _ok(row)

    async def events(self):
        limit = max(1, min(200, _int(_arg("limit", 50), 50)))
        rows = [self._event_row(row) for row in self.plugin.core.recent_events(limit)]
        return _ok({"items": rows})

    async def meta(self):
        plugin = self.plugin
        categories = []
        for name, blurb in _category_blurbs(plugin):
            names = _names_of_category(plugin, name)
            lo, hi = category_range(name)
            categories.append(
                {
                    "name": name,
                    "blurb": blurb,
                    "count": len(names),
                    "names": names,
                    "affection": [lo, hi],
                }
            )
        return _ok(
            {
                "plugin_version": getattr(plugin, "version", ""),
                "data_version": getattr(plugin, "DATA_VERSION_LABEL", 4),
                "categories": categories,
                "phases": [{"name": n, "threshold": t} for n, t in PHASE_STEPS],
                "source_labels": SOURCE_LABELS,
                "bots": plugin.bots(),
                "config": {
                    "panel_enabled": bool(plugin.config.get("panel_enabled", True)),
                    "separate_group_relation": bool(
                        plugin.config.get("separate_group_relation", False)
                    ),
                    "relation_ttl_days": _int(
                        plugin.config.get("relation_ttl_days", 0), 0
                    ),
                    "affection_evolve": bool(
                        plugin.config.get("affection_evolve", True)
                    ),
                    "stat_enabled": bool(plugin.config.get("stat_enabled", True)),
                    "rate_limit_per_min": _int(
                        plugin.config.get("rate_limit_per_min", 0), 0
                    ),
                    "export_to_social": bool(
                        plugin.config.get("export_to_social", False)
                    ),
                    "notify_admin_on_change": bool(
                        plugin.config.get("notify_admin_on_change", False)
                    ),
                },
            }
        )

    async def rules(self):
        rows = []
        for name, blurb in _category_blurbs(self.plugin):
            lo, hi = category_range(name)
            rows.append({"category": name, "blurb": blurb, "affection": [lo, hi]})
        return _ok(
            {
                "rules": rows,
                "phases": [{"name": n, "threshold": t} for n, t in PHASE_STEPS],
                "formula": "好感度 = 类别下限 + (类别上限 − 类别下限) × 熟悉度；熟悉度由互动句数与认识天数取对数收敛后加权。",
                "editable": False,
            }
        )

    async def templates_get(self):
        plugin = self.plugin
        return _ok(
            {
                "current": str(plugin.config.get("hint_template", "") or ""),
                "default": plugin.default_hint_template(),
                "variables": [
                    {"name": "{phrase}", "desc": "视角句，如「我在灰凝面前就是恋人」"},
                    {"name": "{body}", "desc": "关系本身的常态与触发描述"},
                    {"name": "{note}", "desc": "备注（称呼或补充那句）"},
                    {
                        "name": "{meta}",
                        "desc": "元指令（由 meta_level 决定，可能为空）",
                    },
                    {"name": "{relation}", "desc": "关系名"},
                    {"name": "{affection}", "desc": "好感度数字"},
                    {"name": "{phase}", "desc": "阶段名，如「亲近」"},
                ],
            }
        )

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    async def set_relation(self):
        plugin = self.plugin
        payload = await self._json()
        bot = _text(payload.get("bot"), 64) or (
            plugin.bots()[0] if plugin.bots() else "default"
        )
        uid = _text(payload.get("uid"), 64)
        relation = _text(payload.get("relation"), 40)
        note = payload.get("note")
        if not uid:
            return _err("缺少 uid。")
        if not relation:
            return _err("缺少 relation。")
        note_text = None if note is None else str(note)
        ok = await plugin.write_relation(bot, uid, relation, "panel", note_text)
        return _ok(
            {
                "bot": bot,
                "uid": uid,
                "relation": relation,
                "saved": bool(ok),
                "user": self._user_row(bot, uid),
            }
        )

    async def clear_relation(self):
        plugin = self.plugin
        payload = await self._json()
        bot = _text(payload.get("bot"), 64) or (
            plugin.bots()[0] if plugin.bots() else "default"
        )
        uid = _text(payload.get("uid"), 64)
        if not uid:
            return _err("缺少 uid。")
        had = uid in (plugin.data.get(bot, {}) or {})
        ok = await plugin.erase_relation(bot, uid)
        return _ok({"bot": bot, "uid": uid, "had": had, "saved": bool(ok)})

    async def set_note(self):
        plugin = self.plugin
        payload = await self._json()
        bot = _text(payload.get("bot"), 64) or (
            plugin.bots()[0] if plugin.bots() else "default"
        )
        uid = _text(payload.get("uid"), 64)
        note = str(payload.get("note", "") or "")
        if not uid:
            return _err("缺少 uid。")
        relation = str(plugin.data.get(bot, {}).get(uid, "") or "")
        if not relation:
            return _err("这个人还没有关系，先设置关系再写备注。")
        ok = await plugin.write_relation(
            bot, uid, relation, plugin.source_of(bot, uid), note
        )
        return _ok(
            {
                "bot": bot,
                "uid": uid,
                "note": plugin.note_of(bot, uid),
                "saved": bool(ok),
            }
        )

    async def set_group_relation(self):
        plugin = self.plugin
        payload = await self._json()
        bot = _text(payload.get("bot"), 64) or (
            plugin.bots()[0] if plugin.bots() else "default"
        )
        umo = _text(payload.get("umo"), 160)
        uid = _text(payload.get("uid"), 64)
        relation = _text(payload.get("relation"), 40)
        if not (umo and uid):
            return _err("缺少 umo 或 uid。")
        plugin.core.group_set(bot, umo, uid, relation)
        ok = await plugin.save_data()
        plugin.core.push_event(
            "group", bot, uid, f"{umo} 群内关系改为 {relation or '（跟随全局）'}"
        )
        return _ok({"saved": bool(ok), "umo": umo, "relation": relation})

    async def templates_preview(self):
        """拿一份没保存的草稿模板去渲染一个真实用户，不碰配置。"""
        plugin = self.plugin
        payload = await self._json()
        template = str(payload.get("template", "") or "")
        if len(template) > 2000:
            return _err("模板太长了（上限 2000 字）。")
        bot = _text(payload.get("bot"), 64)
        uid = _text(payload.get("uid"), 64)
        if not (bot and uid):
            bot, uid = self._sample_user()
        if not (bot and uid):
            return _err("还没有任何用户关系，先设一条关系再预览。")
        relation = str(plugin.data.get(bot, {}).get(uid, "") or "")
        if not relation:
            return _err("这个人还没有关系，先设置关系再预览。")
        note = plugin.note_of(bot, uid)
        text = ""
        error = ""
        try:
            text = plugin.render_hint(
                relation, note, bot, uid, template=template
            )
        except Exception as exc:  # pragma: no cover - 兜底
            error = str(exc)
        return _ok(
            {
                "bot": bot,
                "uid": uid,
                "relation": relation,
                "note": note,
                "text": text,
                "error": error,
                "fallback": bool(template.strip())
                and not text.startswith("<"),
            }
        )

    def _sample_user(self):
        """挑一个最适合拿来做预览的人：有备注的优先，其次最近活跃的。"""
        plugin = self.plugin
        best = ("", "", -1)
        for bot in plugin.bots():
            for uid in plugin.data.get(bot, {}) or {}:
                note = plugin.note_of(bot, uid)
                stat = plugin.core.stat_of(bot, uid) or {}
                score = _int(stat.get("last"), 0)
                if note:
                    score += 10**12
                if score > best[2]:
                    best = (bot, uid, score)
        return best[0], best[1]

    async def templates_save(self):
        plugin = self.plugin
        payload = await self._json()
        template = str(payload.get("template", "") or "")
        if len(template) > 2000:
            return _err("模板太长了（上限 2000 字）。")
        plugin.config["hint_template"] = template
        plugin.core.push_event("config", "", "", "提示词模板已更新")
        persisted = False
        error = ""
        saver = getattr(plugin.config, "save_config_async", None)
        if callable(saver):
            try:
                persisted = bool(await saver({"hint_template": template}))
            except Exception as exc:  # pragma: no cover - 框架差异
                error = str(exc)
        if not persisted:
            saver = getattr(plugin.config, "save_config", None)
            if callable(saver):
                try:
                    saver({"hint_template": template})
                    persisted = True
                except Exception as exc:  # pragma: no cover - 框架差异
                    error = str(exc)
        return _ok(
            {
                "template": template,
                "persisted": persisted,
                "error": error,
                "note": "模板已保存到插件配置，重启后仍生效。"
                if persisted
                else "模板已在本次运行内生效，但没能写回配置文件（%s）；要长期生效请在 AstrBot 配置面板里改 hint_template。"
                % (error or "当前框架没提供保存接口"),
            }
        )

    async def export_status(self):
        return _ok(self.plugin.exporter.status())

    async def export_preview(self):
        try:
            data = self.plugin.exporter.preview()
        except Exception as exc:
            return _err(f"组装失败：{exc}", 500)
        return _ok({"payload": data, "stale_seconds": CONTRACT_STALE_SECONDS})

    async def export_run(self):
        payload = await self._json()
        force = bool(payload.get("force", False))
        ok = self.plugin.exporter.write(force=force)
        return _ok({"written": bool(ok), "status": self.plugin.exporter.status()})

    # ------------------------------------------------------------------
    # 行装配
    # ------------------------------------------------------------------
    def _user_row(self, bot: str, uid: str) -> Dict[str, Any]:
        plugin = self.plugin
        core = plugin.core
        relation = str(plugin.data.get(bot, {}).get(uid, "") or "")
        category = relation_category_of(plugin, relation)
        note = plugin.note_of(bot, uid)
        src = plugin.source_of(bot, uid)
        affection = core.affection(bot, uid, relation, category) if relation else 0
        phase = phase_of(affection) if relation else ""
        stat = core.stat_of(bot, uid) or {}
        ttl_days = _int(plugin.config.get("relation_ttl_days", 0), 0)
        return {
            "bot": bot,
            "uid": uid,
            "relation": relation,
            "category": category or ("自定义" if relation else ""),
            "note": note,
            "nickname": note if note and len(note) <= 12 else uid,
            "src": src,
            "src_text": SOURCE_LABELS.get(src, src),
            "at": _int(plugin.meta_of(bot, uid).get("at"), 0),
            "at_text": _fmt_time(plugin.meta_of(bot, uid).get("at")),
            "affection": affection,
            "phase": phase,
            "count": _int(stat.get("n"), 0),
            "first_at": _int(stat.get("first"), 0),
            "first_text": _fmt_time(stat.get("first")),
            "last_at": _int(stat.get("last"), 0),
            "last_text": _fmt_time(stat.get("last")),
            "expired": bool(ttl_days > 0 and core.expired(bot, uid, ttl_days)),
        }

    def _event_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "at": row.get("at", 0),
            "at_text": _fmt_time(row.get("at")),
            "kind": row.get("kind", ""),
            "bot": row.get("bot", ""),
            "uid": row.get("uid", ""),
            "text": row.get("text", ""),
        }

    def _hint_preview(self, relation: str, note: str) -> str:
        try:
            return self.plugin.render_hint(relation, note)
        except Exception:
            return ""

    async def _json(self) -> Dict[str, Any]:
        try:
            payload = await _body()
        except Exception:
            payload = None
        return payload if isinstance(payload, dict) else {}
