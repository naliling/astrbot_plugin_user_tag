"""user_tag v2.5.0 的自测。没有 pytest 也能跑：

    python tests/run_tests.py

它不连真 AstrBot：这里把 `astrbot.*` 与 `quart` 用内存里的假模块顶掉，
然后直接 import 插件本体。目的是把「改了关系之后存档里到底变成了什么」
「导出的 state.json 是否符合 autonomous_social 的契约」这类事钉死，
而不是去测框架。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 假框架
_PASSED = []
_FAILED = []


def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


def _deco(*args, **kwargs):
    """既能当 @filter.command("x") 用，也能当 @filter.on_llm_request 用。"""
    if len(args) == 1 and not kwargs and inspect.isfunction(args[0]):
        return args[0]

    def wrap(fn):
        return fn

    return wrap


class _Filter:
    command = staticmethod(_deco)
    command_group = staticmethod(_deco)
    custom_filter = staticmethod(_deco)
    on_llm_request = staticmethod(_deco)
    llm_tool = staticmethod(_deco)
    event_message_type = staticmethod(_deco)

    class EventMessageType:
        ALL = "all"


class CustomFilter:
    def __init__(self, *a, **k):
        pass

    def filter(self, event, *a, **k):
        return True


class FakeEvent:
    def __init__(
        self,
        sender="10001",
        self_id="bot1",
        umo="aiocqhttp:FriendMessage:10001",
        text="",
        admin=False,
    ):
        self._sender = sender
        self._self_id = self_id
        self._umo = umo
        self._text = text
        self._admin = admin
        self.stopped = False
        self._extras = {}
        # 真框架里 unified_msg_origin 是属性（不是方法），插件也是按属性取的。
        self.unified_msg_origin = umo

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def get_sender_id(self):
        return self._sender

    def get_self_id(self):
        return self._self_id

    def get_message_str(self):
        return self._text

    def get_session_id(self):
        return self._umo

    def get_group_id(self):
        return ""

    def is_admin(self):
        return self._admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def should_call_llm(self, *a, **k):
        return None

    def __getattr__(self, name):
        # 插件里还会摸别的属性，一律给个无害的空壳，免得测试被无关细节绊住。
        return lambda *a, **k: None


class FakeContext:
    def __init__(self):
        self.routes = []
        self.sent = []

    def register_web_api(self, path, handler, methods, desc=""):
        self.routes.append(
            {"path": path, "handler": handler, "methods": methods, "desc": desc}
        )

    def unregister_web_api(self, path):
        self.routes = [r for r in self.routes if r["path"] != path]

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class Star:
    def __init__(self, context=None, config=None):
        self.context = context
        self.name = "astrbot_plugin_user_tag"


class _Logger:
    def __init__(self):
        self.lines = []

    def _log(self, level, msg, *a):
        try:
            self.lines.append((level, msg % a if a else msg))
        except Exception:
            self.lines.append((level, msg))

    def debug(self, msg, *a):
        self._log("debug", msg, *a)

    def info(self, msg, *a):
        self._log("info", msg, *a)

    def warning(self, msg, *a):
        self._log("warning", msg, *a)

    def error(self, msg, *a):
        self._log("error", msg, *a)


class AstrBotConfig(dict):
    pass


class TextPart:
    def __init__(self, text="", type="text", **k):
        self.text = text
        self.type = type


class ProviderRequest:
    def __init__(self):
        self.extra_user_content_parts = []


class At:
    def __init__(self, qq=None, name=None):
        self.qq = qq
        self.name = name


class Plain:
    def __init__(self, text=""):
        self.text = text


class _QuartRequest:
    args = {}
    headers = {}
    _body = None

    async def get_json(self, silent=False):
        return self._body


def jsonify(obj):
    class _Resp(dict):
        def get_json(self):
            return dict(self)

    return _Resp(obj if isinstance(obj, dict) else {"data": obj})


# --- 新版 AstrBot 的 astrbot.api.web 接口面（两套都要测）---------------
class _WebQuery:
    """对应 astrbot.api.web.request.query：get(name, default, type=) / getlist。"""

    def __init__(self):
        self._d = {}

    def get(self, name, default=None, type=None):  # noqa: A002 - 对齐官方签名
        value = self._d.get(name, default)
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default

    def getlist(self, name):
        value = self._d.get(name)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


class _WebRequest:
    method = "GET"
    path = "/api/v1/plugins/extensions/astrbot_plugin_user_tag/overview"
    plugin_name = "astrbot_plugin_user_tag"
    username = "tester"
    headers = {}
    cookies = {}
    content_type = "application/json"
    client_host = "127.0.0.1"
    path_params = {}

    def __init__(self):
        self.query = _WebQuery()
        self._body = None

    async def json(self, default=None):
        if self._body is not None:
            return self._body
        return default if default is not None else {}

    async def body(self):
        return b""


def json_response(obj, status_code: int = 200):
    return jsonify(obj)


def error_response(message: str, status_code: int = 400):
    return jsonify({"ok": False, "error": str(message), "code": status_code})


def install_stubs(data_root: str, with_web_api: bool = True):
    astrbot = mod("astrbot")
    api = mod("astrbot.api", logger=_Logger(), AstrBotConfig=AstrBotConfig)
    event_mod = mod("astrbot.api.event", AstrMessageEvent=FakeEvent)
    filter_mod = mod(
        "astrbot.api.event.filter",
        filter=_Filter(),
        CustomFilter=CustomFilter,
        GreedyStr=str,
    )
    event_mod.filter = filter_mod.filter
    mod("astrbot.api.star", Star=Star, Context=FakeContext)
    mod("astrbot.api.message_components", At=At, Plain=Plain)

    core = mod("astrbot.core")
    utils = mod("astrbot.core.utils")
    mod("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: data_root)
    provider = mod("astrbot.core.provider")
    mod("astrbot.core.provider.entities", ProviderRequest=ProviderRequest)
    agent = mod("astrbot.core.agent")
    mod("astrbot.core.agent.message", TextPart=TextPart)

    astrbot.api = api
    api.event = event_mod
    astrbot.core = core
    core.utils = utils
    core.provider = provider
    core.agent = agent
    utils.astrbot_path = sys.modules["astrbot.core.utils.astrbot_path"]
    provider.entities = sys.modules["astrbot.core.provider.entities"]
    agent.message = sys.modules["astrbot.core.agent.message"]

    mod("quart", jsonify=jsonify, request=_QuartRequest())

    if with_web_api:
        web = mod(
            "astrbot.api.web",
            json_response=json_response,
            error_response=error_response,
            request=_WebRequest(),
            PluginUploadFile=object,
        )
        api.web = web


def load_plugin_module():
    """main.py 用的是相对导入，得先给它造一个包名。"""
    pkg_name = "user_tag_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[pkg_name] = pkg
    return importlib.import_module(pkg_name + ".main")


# ---------------------------------------------------------------- 断言
_TAG = ""


async def check(name, fn):
    if _TAG:
        name = "[%s] %s" % (_TAG, name)
    try:
        result = fn()
        if inspect.isawaitable(result):
            await result
        _PASSED.append(name)
        print("  ok   " + name)
    except AssertionError as exc:
        _FAILED.append((name, str(exc) or "断言失败"))
        print("  FAIL " + name + " :: " + (str(exc) or "断言失败"))
    except Exception as exc:  # noqa: BLE001
        import traceback

        _FAILED.append((name, traceback.format_exc()))
        print("  ERR  " + name + " :: " + repr(exc))


def eq(actual, expected, label=""):
    if actual != expected:
        raise AssertionError("%s 期望 %r，实际 %r" % (label, expected, actual))


def truthy(value, label=""):
    if not value:
        raise AssertionError("%s 期望为真，实际 %r" % (label, value))


def _req():
    """拿到当前模式下的 request 代理（新版 astrbot.api.web 或 quart）。"""
    if "astrbot.api.web" in sys.modules:
        return sys.modules["astrbot.api.web"].request
    return sys.modules["quart"].request


def set_query(**kwargs):
    r = _req()
    if hasattr(r, "query"):
        r.query._d = dict(kwargs)
    else:
        r.args = dict(kwargs)


def set_body(payload):
    _req()._body = payload


# ---------------------------------------------------------------- 主体
async def run(with_web_api: bool = True):
    global _TAG
    _TAG = "astrbot.api.web" if with_web_api else "quart"
    data_root = tempfile.mkdtemp(prefix="user_tag_test_")
    os.environ["USER_TAG_TEST_DATA"] = data_root
    install_stubs(data_root, with_web_api=with_web_api)
    main = load_plugin_module()

    config = AstrBotConfig(
        {
            "admin_qq": "",
            "default_relation": "",
            "enable_default_relation": False,
            "max_note_len": 60,
            "meta_level": 1,
            "fuzzy_match": True,
            "allow_model_tool": True,
            "require_confirm": False,
            "session_filter_mode": "off",
            "session_list": [],
            "panel_enabled": True,
            "panel_token": "",
            "stat_enabled": True,
            "affection_evolve": True,
            "separate_group_relation": False,
            "relation_ttl_days": 0,
            "rate_limit_per_min": 0,
            "hint_template": "",
            "notify_admin_on_change": False,
            "notify_umo": "",
            "export_to_social": True,
            "export_interval_min": 20,
            "export_target_path": "astrbot_plugin_humanoid_core/state.json",
        }
    )

    ctx = FakeContext()
    plugin = main.UserTagPlugin(ctx, config)
    ev = FakeEvent()

    # ---- 1 基本结构
    def t_structure():
        truthy(hasattr(plugin, "core"), "core")
        truthy(hasattr(plugin, "exporter"), "exporter")
        eq(main.DATA_VERSION, 4, "DATA_VERSION")

    await check("插件初始化出 v4 运行时", t_structure)

    # ---- 2 设关系：数据 / 历史 / 事件 / 统计
    async def set_relation():
        text = await plugin.do_set(ev, "恋人")
        eq(plugin.data["bot1"]["10001"], "恋人", "落库关系")
        eq(len(plugin.core.hist_of("bot1", "10001")), 1, "历史条数")
        truthy(plugin.core.recent_events(10), "事件流")
        return text

    await check("设关系会写历史与事件", set_relation)

    # ---- 3 注入块与旧行为一致
    def t_hint_default():
        expected = main.build_hint("恋人", "", 1)
        eq(plugin.render_hint("恋人", ""), expected, "默认模板")

    await check("没配模板时注入块与旧版逐字相同", t_hint_default)

    # ---- 4 自定义模板
    def t_hint_custom():
        plugin.config["hint_template"] = "R={relation}|A={affection}|P={phase}"
        got = plugin.render_hint("恋人", "", "bot1", "10001")
        truthy(got.startswith("R=恋人|A="), got)
        truthy("|P=" in got, got)

    await check("自定义模板能取到关系/好感度/阶段", t_hint_custom)

    # ---- 5 模板写坏要回退，不能把注入吞掉
    def t_hint_broken():
        plugin.config["hint_template"] = "R={relation}|BAD={nope}"
        got = plugin.render_hint("恋人", "")
        eq(got, main.build_hint("恋人", "", plugin.meta_level()), "回退默认")

    await check("模板写坏变量名自动回退", t_hint_broken)

    # ---- 6 有效期
    def t_ttl():
        plugin.config["hint_template"] = ""
        plugin.config["relation_ttl_days"] = 1
        plugin.core.bump("bot1", "10001")
        plugin.core.stats["bot1"]["10001"]["last"] = (
            int(asyncio.get_event_loop().time()) - 3 * 86400
        )
        import time as _t

        plugin.core.stats["bot1"]["10001"]["last"] = int(_t.time()) - 3 * 86400
        relation, note, _label = plugin.effective(ev)
        eq(relation, "", "过期后不再注入")
        plugin.config["relation_ttl_days"] = 0
        relation2, _n, _l = plugin.effective(ev)
        eq(relation2, "恋人", "关掉有效期后恢复")

    await check("超过有效期就不注入（数据不删）", t_ttl)

    # ---- 7 群内覆盖
    def t_group():
        plugin.config["separate_group_relation"] = True
        plugin.core.group_set("bot1", ev.unified_msg_origin, "10001", "同事")
        relation, _n, label = plugin.effective(ev)
        eq(relation, "同事", "群内关系优先")
        eq(label, "本群设定", "来源标签")
        plugin.config["separate_group_relation"] = False
        relation2, _n2, _l2 = plugin.effective(ev)
        eq(relation2, "恋人", "关掉后回到全局")

    await check("群内独立关系可覆盖全局", t_group)

    # ---- 8 存档里真的有新字段
    async def t_save():
        await plugin.save_data()
        raw = json.loads(Path(plugin.data_file).read_text(encoding="utf-8"))
        eq(raw["v"], 4, "存档版本")
        for key in ("stats", "hist", "events", "group_rel"):
            truthy(key in raw, "存档缺少 " + key)
        return raw

    await check("存档带上统计/历史/事件/群内覆盖", t_save)

    # ---- 9 重新加载能读回来
    def t_reload():
        fresh = main.UserTagPlugin(FakeContext(), AstrBotConfig(dict(config)))
        eq(fresh.data["bot1"]["10001"], "恋人", "重载关系")
        truthy(fresh.core.count_of("bot1", "10001") >= 1, "重载统计")
        truthy(fresh.core.hist_of("bot1", "10001"), "重载历史")

    await check("重启后统计与历史不丢", t_reload)

    # ---- 10 导出契约
    def t_export():
        ok = plugin.exporter.write(force=True)
        truthy(ok, "导出应成功")
        target = plugin.exporter.target_path()
        truthy(target.exists(), "目标文件不存在")
        data = json.loads(target.read_text(encoding="utf-8"))
        truthy(str(data["schema"]).startswith("humanoid.contract/"), "schema 前缀")
        eq(data["schema"], "humanoid.contract/1", "schema 版本")
        truthy("roles" in data and "bot1" in data["roles"], "roles.bot1")
        user = data["roles"]["bot1"]["users"]["10001"]
        eq(user["relation"], "恋人", "导出关系")
        truthy(isinstance(user["mood"]["affection"], int), "affection 应为整数")
        body = data["roles"]["bot1"]["self"]["contract"]["body"]
        for key in ("energy", "asleep", "social_desire"):
            truthy(key in body, "contract.body 缺 " + key)
        caps = data["roles"]["bot1"]["self"]["contract"]["capabilities"]
        eq(len(caps), 7, "capabilities 应为 7 项")

    await check("导出的 state.json 符合 autonomous_social 契约", t_export)

    # ---- 10b 导出有效性：autonomous_social 取第一个存在的文件，得说清楚会不会被读到
    def t_export_effective():
        status = plugin.exporter.status()
        truthy("effective" in status, "status 应包含 effective")
        truthy("read_path" in status, "status 应包含 read_path")
        eq(status["effective"], True, "只写了默认目标时应该能被读到")
        truthy(
            status["read_path"].replace("\\", "/").endswith(
                "astrbot_plugin_humanoid_core/state.json"
            ),
            "read_path 应指向默认目标",
        )
        # 造一个 Humanoid Core 本体的文件：它优先级更高，导出应自认无效
        hc = plugin.data_file.parent.parent / "humanoid_core" / "state.json"
        hc.parent.mkdir(parents=True, exist_ok=True)
        hc.write_text('{"roles": {}}', encoding="utf-8")
        status = plugin.exporter.status()
        eq(status["effective"], False, "HC 占着优先级更高的文件时应自认无效")
        truthy(status["occupied_by"], "应报出被谁占着")
        hc.unlink()

    await check("导出会判断自己会不会真被读到", t_export_effective)

    # ---- 11 fields_hash 必须能被对方算回来
    def t_hash():
        from user_tag_pkg.relexport import _fields_hash

        data = plugin.exporter.preview()
        stored = data.pop("fields_hash", None)
        truthy(stored, "缺少 fields_hash")
        eq(_fields_hash(data), stored, "fields_hash 不自洽")

    await check("fields_hash 与对方算法一致", t_hash)

    # ---- 12 面板路由
    def t_routes():
        eq(len(ctx.routes), 16, "面板接口数")
        paths = sorted(r["path"] for r in ctx.routes)
        truthy(any(p.endswith("/overview") for p in paths), "缺少 overview")
        truthy(any(p.endswith("/export/run") for p in paths), "缺少 export/run")

    await check("面板注册了 16 个接口", t_routes)

    # ---- 13 面板读接口真的能跑
    async def t_panel_read():
        set_query()
        handler = None
        for r in ctx.routes:
            if r["path"].endswith("/overview"):
                handler = r["handler"]
        truthy(handler, "找不到 overview 处理函数")
        resp = await handler()
        body = resp.get_json() if hasattr(resp, "get_json") else resp
        truthy(body.get("ok"), "overview 返回 ok")
        data = body["data"]
        truthy(data["total_users"] >= 1, "总人数")
        truthy(data["bots"] >= 1, "机器人列表")

    await check("面板 overview 接口可调用", t_panel_read)

    # ---- 14 面板写接口走的是同一条落库路
    async def t_panel_write():
        set_query()
        set_body({"bot": "bot1", "uid": "10001", "relation": "挚友"})
        handler = None
        for r in ctx.routes:
            if r["path"].endswith("/set"):
                handler = r["handler"]
        truthy(handler, "找不到 set 处理函数")
        resp = await handler()
        body = resp.get_json() if hasattr(resp, "get_json") else resp
        truthy(body.get("ok"), "面板写失败")
        eq(plugin.data["bot1"]["10001"], "挚友", "面板改完应落库")
        truthy(len(plugin.core.hist_of("bot1", "10001")) >= 2, "面板改动也要进历史")

    await check("面板改关系会走 write_relation 并留痕", t_panel_write)

    # ---- 14b 规则/元信息接口：模块级常量（CATEGORY_BLURBS / names_of_category /
    #          relation_category）在 main.py 里，不在插件实例上。取不到就是静默空值，
    #          真机表现是「所有人都被归成自定义」——这条就是防它的回归。
    async def t_panel_module_level():
        def handler_of(suffix):
            for r in ctx.routes:
                if r["path"].endswith(suffix):
                    return r["handler"]
            return None

        async def payload(fn):
            resp = await fn()
            return resp.get_json() if hasattr(resp, "get_json") else resp

        set_query()
        body = await payload(handler_of("/rules"))
        truthy(body.get("ok"), "rules 返回 ok")
        rows = body["data"]["rules"]
        eq(len(rows), 9, "应有 9 个类别")
        truthy(all(r["blurb"] for r in rows), "每个类别都要有说明")

        body = await payload(handler_of("/meta"))
        truthy(body.get("ok"), "meta 返回 ok")
        cats = body["data"]["categories"]
        eq(sum(c["count"] for c in cats), 139, "139 条预设关系应全部归入类别")

        set_query(bot="bot1", uid="10001")
        body = await payload(handler_of("/user"))
        truthy(body.get("ok"), "user 返回 ok")
        eq(body["data"]["category"], "日常", "挚友在预设表里归日常类")

    await check("规则/元信息接口取得到模块级常量", t_panel_module_level)

    # ---- 16 面板页面文件不能挡住桥注入
    async def t_panel_html_bridge():
        path = PLUGIN_DIR / "views" / "关系面板" / "index.html"
        truthy(path.is_file(), "页面文件应该在 views/关系面板/index.html")
        html = path.read_text(encoding="utf-8")
        truthy(
            "bridge-sdk" not in html.lower(),
            "index.html 里出现桥脚本地址会让 AstrBot 跳过自动注入，页面拿不到 window.AstrBotPluginView",
        )

    await check("面板页面不阻断桥脚本注入", t_panel_html_bridge)

    # ---- 17 模板预览接口
    async def t_templates_preview():
        def handler_of(suffix):
            for r in ctx.routes:
                if r["path"].endswith(suffix):
                    return r["handler"]
            return None

        async def payload(fn):
            resp = await fn()
            return resp.get_json() if hasattr(resp, "get_json") else resp

        set_query()
        set_body({"template": "<rel>{relation}|{affection}|{phase}</rel>"})
        body = await payload(handler_of("/templates/preview"))
        truthy(body.get("ok"), "预览应返回 ok")
        text = body["data"]["text"]
        truthy("<rel>" in text and "|" in text, "模板应被套用，实际：" + text)

        set_body({"template": "{nope}"})
        body = await payload(handler_of("/templates/preview"))
        truthy(body.get("ok"), "坏模板也应返回 ok")
        truthy(
            body["data"]["text"].startswith("<relation>"),
            "坏模板应回退默认，实际：" + body["data"]["text"],
        )

    await check("模板预览不碰配置且坏模板会回退", t_templates_preview)

    # ---- 15 限流
    async def t_rate():
        plugin.config["rate_limit_per_min"] = 2
        plugin.core.rate_reset()
        first = await plugin.do_set(ev, "朋友")
        second = await plugin.do_set(ev, "同事")
        third = await plugin.do_set(ev, "同学")
        truthy(first and second, "前两次应放行")
        truthy("慢一点" in third, "第三次应被限流，实际：" + third)
        plugin.config["rate_limit_per_min"] = 0

    await check("限流生效且不影响前两次", t_rate)

    # ---- 16 清关系也要留痕
    async def t_clear():
        before = len(plugin.core.hist_of("bot1", "10001"))
        await plugin.erase_relation("bot1", "10001")
        eq(plugin.data["bot1"].get("10001"), None, "关系应被清掉")
        truthy(len(plugin.core.hist_of("bot1", "10001")) > before, "清除也要进历史")

    await check("清关系会写一条历史", t_clear)

    # ---- 17 看用法不该占限流额度
    async def t_usage_free():
        plugin.config["rate_limit_per_min"] = 1
        plugin.core.rate_reset()
        a = await plugin.do_set(ev, "")
        b = await plugin.do_set(ev, "")
        truthy("设置关系" in a and "设置关系" in b, "看用法应一直给说明，实际：" + a)
        truthy("慢一点" not in a and "慢一点" not in b, "看用法不该被限流")
        ok = await plugin.do_set(ev, "朋友")
        truthy("慢一点" not in ok, "看两次用法之后仍应能设置一次，实际：" + ok)
        blocked = await plugin.do_set(ev, "同事")
        truthy("慢一点" in blocked, "真改过一次之后才该限流，实际：" + blocked)
        plugin.config["rate_limit_per_min"] = 0
        plugin.core.rate_reset()

    await check("看用法不占限流额度", t_usage_free)

    # ---- 18 一条消息只回一次
    async def t_once():
        e = FakeEvent(sender="10001", text="设置关系 恋人")
        first = [x async for x in plugin.set_relation(e, "恋人")]
        second = [x async for x in plugin.bare_command(e)]
        truthy(len(first) == 1, "第一个入口应回一条，实际 " + str(len(first)))
        eq(second, [], "第二个入口同一条消息里不该再回")
        e2 = FakeEvent(sender="10001", text="关系帮助")
        one = [x async for x in plugin.bare_command(e2)]
        truthy(len(one) == 1, "单独发裸指令应照常回一条")

    await check("一条消息只回一次", t_once)

    # ---- 19 设成一样的东西不扣额度、不改数据
    async def t_same():
        plugin.config["rate_limit_per_min"] = 1
        plugin.core.rate_reset()
        first = await plugin.do_set(ev, "同事")
        again = await plugin.do_set(ev, "同事")
        truthy("慢一点" not in first, "第一次应成功，实际：" + first)
        truthy("本来就是" in again, "重复设同一条应说没动，实际：" + again)
        eq(plugin.data["bot1"]["10001"], "同事", "数据不该被再写一次")
        plugin.config["rate_limit_per_min"] = 0
        plugin.core.rate_reset()

    await check("设成一样的东西不扣额度", t_same)

    shutil.rmtree(data_root, ignore_errors=True)


def _purge():
    """两轮之间把桩和插件模块清干净，否则第二轮拿到的是上一轮的模块。"""
    for name in list(sys.modules):
        if (
            name == "quart"
            or name == "astrbot"
            or name.startswith("astrbot.")
            or name == "user_tag_pkg"
            or name.startswith("user_tag_pkg.")
        ):
            del sys.modules[name]


def main_entry():
    print("user_tag 自测")
    for with_web_api in (True, False):
        _purge()
        asyncio.run(run(with_web_api=with_web_api))
    print("")
    print("通过 %d / %d" % (len(_PASSED), len(_PASSED) + len(_FAILED)))
    if _FAILED:
        for name, err in _FAILED:
            print("--- " + name)
            print(err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
