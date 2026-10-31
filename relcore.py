"""关系数据层 v4。

这一层只做四件事：把「关系」从一个静态字符串，变成一条有历史、有分数、
有统计、有寿命的记录；其余模块（面板、导出、指令）都只读它。

设计原则（和主文件一致）：
1. 只增字段，不动 relations 的形状 —— 老存档原样能读，读坏了大不了回空。
2. 所有写操作都走主文件的 save_data()，这里不碰磁盘。
3. 任何统计/演进失败都不许影响「设置关系」本身：算分是锦上添花，改关系是正事。
"""

from __future__ import annotations

import math
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

CORE_VERSION = 4
HIST_MAX = 60  # 每个用户最多留多少条变更历史
EVENT_MAX = 300  # 面板「最近变更」最多留多少条
STAT_DAYS_MAX = 90  # 日统计保留天数


def plugin_module(plugin: Any) -> Any:
    """拿到插件自己那个模块。

    main.py 里有几个东西是**模块级**的：CATEGORY_BLURBS、names_of_category()、
    relation_category()。它们不在插件实例上，写 plugin.xxx 会 AttributeError；
    外面再套个 try/except，就变成「静默空值」——真机上的表现就是面板里
    所有人都被归成「自定义」类别，导出里的 relation_category 也全空。
    """
    module = sys.modules.get(type(plugin).__module__)
    return module if module is not None else plugin


def relation_category_of(plugin: Any, relation: str) -> str:
    """调 main.py 的模块级 relation_category()，取不到就回空串。"""
    fn = getattr(plugin_module(plugin), "relation_category", None)
    if not callable(fn):
        fn = getattr(plugin, "relation_category", None)
    if not callable(fn) or not relation:
        return ""
    try:
        return str(fn(relation) or "")
    except Exception:  # noqa: BLE001 - 分类算不出来不影响主流程
        return ""


# 关系类别 -> 好感度基础区间。同一类关系里的人，起点不一样才像回事：
# 恋人不会一上来就 30 分，同事也不会一上来就 90 分。
CATEGORY_AFFECTION: Dict[str, Tuple[int, int]] = {
    "日常": (30, 45),
    "亲友": (60, 75),
    "恋爱": (80, 92),
    "心动": (65, 78),
    "恋爱设定": (82, 95),
    "身份": (35, 55),
    "主仆": (50, 70),
    "奇幻": (55, 75),
    "玩梗": (45, 65),
}
CUSTOM_AFFECTION: Tuple[int, int] = (40, 60)

# 好感度 -> 阶段。只用于展示与「可升级」提示，不自动改关系名。
PHASE_STEPS: List[Tuple[str, int]] = [
    ("陌生", 0),
    ("眼熟", 20),
    ("熟识", 40),
    ("亲近", 58),
    ("亲密", 76),
    ("羁绊", 90),
]

# 变更来源的中文标签（面板与事件流共用）
SOURCE_LABELS: Dict[str, str] = {
    "user": "本人设置",
    "admin": "管理员代管",
    "model": "AI 自行判断",
    "panel": "面板修改",
    "evolve": "自动演进",
    "system": "系统",
}


def today_key(now: Optional[float] = None) -> str:
    """本地日期键，YYYY-MM-DD。用本地时间是因为管理员看的是本地日历。"""
    return time.strftime(
        "%Y-%m-%d", time.localtime(now if now is not None else time.time())
    )


def phase_of(affection: int) -> str:
    name = PHASE_STEPS[0][0]
    for label, threshold in PHASE_STEPS:
        if affection >= threshold:
            name = label
    return name


def next_phase(affection: int) -> Optional[Tuple[str, int]]:
    """返回 (下一阶段名, 还差多少分)；已到顶返回 None。"""
    for label, threshold in PHASE_STEPS:
        if affection < threshold:
            return label, threshold - affection
    return None


def category_range(category: str) -> Tuple[int, int]:
    return CATEGORY_AFFECTION.get(category, CUSTOM_AFFECTION)


def familiarity(count: int, days: float) -> float:
    """把「聊了多少句」和「认识了多少天」揉成一个 0~1 的熟悉度。

    两个都用对数收敛：前 50 句涨得最快，之后越来越慢；认识 60 天封顶。
    这样「今天刚设关系就聊了 200 句」不会直接顶格。
    """
    by_count = math.log1p(max(0, count)) / math.log1p(400.0)
    by_days = math.log1p(max(0.0, days)) / math.log1p(60.0)
    return max(0.0, min(1.0, 0.62 * by_count + 0.38 * by_days))


class RelCore:
    """挂在 UserTagPlugin 上的一份运行时状态。持久化由主文件负责。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        # bot -> uid -> {"n": 总句数, "first": 首次, "last": 最近, "days": {"YYYY-MM-DD": n}}
        self.stats: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # bot -> uid -> [{"at","from","to","by","note"}]
        self.hist: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        # 扁平事件流，最新的在前
        self.events: List[Dict[str, Any]] = []
        # 群维度覆盖：bot -> umo -> uid -> 关系名
        self.group_rel: Dict[str, Dict[str, Dict[str, str]]] = {}
        # 限流：key -> [时间戳]
        self._rate: Dict[str, List[float]] = {}

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.stats = {}
        self.hist = {}
        self.events = []
        self.group_rel = {}
        self._rate = {}

    def adopt(self, raw: Any) -> None:
        """从磁盘 payload 里把 v4 的部分捞出来。认不出的一律当空，不抛。"""
        self.reset()
        if not isinstance(raw, dict):
            return

        stats = raw.get("stats")
        if isinstance(stats, dict):
            for bot, users in stats.items():
                if not isinstance(users, dict):
                    continue
                for uid, item in users.items():
                    if not isinstance(item, dict):
                        continue
                    try:
                        n = int(item.get("n", 0) or 0)
                    except (TypeError, ValueError):
                        n = 0
                    days_raw = item.get("days")
                    days: Dict[str, int] = {}
                    if isinstance(days_raw, dict):
                        for k, v in days_raw.items():
                            try:
                                days[str(k)] = int(v)
                            except (TypeError, ValueError):
                                continue
                    self.stats.setdefault(str(bot), {})[str(uid)] = {
                        "n": max(0, n),
                        "first": _as_int(item.get("first")),
                        "last": _as_int(item.get("last")),
                        "days": days,
                    }

        hist = raw.get("hist")
        if isinstance(hist, dict):
            for bot, users in hist.items():
                if not isinstance(users, dict):
                    continue
                for uid, rows in users.items():
                    if not isinstance(rows, list):
                        continue
                    clean: List[Dict[str, Any]] = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        clean.append(
                            {
                                "at": _as_int(row.get("at")) or 0,
                                "from": str(row.get("from") or ""),
                                "to": str(row.get("to") or ""),
                                "by": str(row.get("by") or "user"),
                                "note": str(row.get("note") or ""),
                            }
                        )
                    if clean:
                        self.hist.setdefault(str(bot), {})[str(uid)] = clean[-HIST_MAX:]

        events = raw.get("events")
        if isinstance(events, list):
            for row in events:
                if isinstance(row, dict):
                    self.events.append(
                        {
                            "at": _as_int(row.get("at")) or 0,
                            "kind": str(row.get("kind") or ""),
                            "bot": str(row.get("bot") or ""),
                            "uid": str(row.get("uid") or ""),
                            "text": str(row.get("text") or ""),
                        }
                    )
            self.events = self.events[-EVENT_MAX:]

        group_rel = raw.get("group_rel")
        if isinstance(group_rel, dict):
            for bot, umos in group_rel.items():
                if not isinstance(umos, dict):
                    continue
                for umo, users in umos.items():
                    if not isinstance(users, dict):
                        continue
                    kept = {
                        str(uid): str(rel)
                        for uid, rel in users.items()
                        if isinstance(rel, str) and rel.strip()
                    }
                    if kept:
                        self.group_rel.setdefault(str(bot), {})[str(umo)] = kept

    def payload(self) -> Dict[str, Any]:
        return {
            "stats": self.stats,
            "hist": self.hist,
            "events": self.events[-EVENT_MAX:],
            "group_rel": self.group_rel,
        }

    # ------------------------------------------------------------------
    # 互动统计
    # ------------------------------------------------------------------
    def bump(self, bot: str, uid: str, now: Optional[float] = None) -> bool:
        """给这次互动记一笔。返回 True 表示是新的一天（面板可用来算活跃度）。"""
        now = now if now is not None else time.time()
        entry = self.stats.setdefault(str(bot), {}).setdefault(
            str(uid), {"n": 0, "first": int(now), "last": int(now), "days": {}}
        )
        entry["n"] = int(entry.get("n", 0)) + 1
        entry["last"] = int(now)
        if not entry.get("first"):
            entry["first"] = int(now)
        days = entry.setdefault("days", {})
        key = today_key(now)
        days[key] = int(days.get(key, 0)) + 1
        if len(days) > STAT_DAYS_MAX:
            for stale in sorted(days)[: len(days) - STAT_DAYS_MAX]:
                days.pop(stale, None)
        return days[key] == 1

    def stat_of(self, bot: str, uid: str) -> Dict[str, Any]:
        return self.stats.get(str(bot), {}).get(str(uid), {})

    def count_of(self, bot: str, uid: str) -> int:
        try:
            return int(self.stat_of(bot, uid).get("n", 0))
        except (TypeError, ValueError):
            return 0

    def days_since_first(
        self, bot: str, uid: str, now: Optional[float] = None
    ) -> float:
        first = _as_int(self.stat_of(bot, uid).get("first"))
        if not first:
            return 0.0
        now = now if now is not None else time.time()
        return max(0.0, (now - first) / 86400.0)

    def recent_counts(
        self, bot: str, uid: str, days: int = 7, now: Optional[float] = None
    ) -> List[Tuple[str, int]]:
        """最近 N 天的 (日期, 句数)，缺的那天补 0，方便直接画柱状图。"""
        now = now if now is not None else time.time()
        bucket = self.stat_of(bot, uid).get("days", {})
        out: List[Tuple[str, int]] = []
        for back in range(days - 1, -1, -1):
            key = today_key(now - back * 86400)
            try:
                out.append((key, int(bucket.get(key, 0))))
            except (TypeError, ValueError):
                out.append((key, 0))
        return out

    # ------------------------------------------------------------------
    # 变更历史 / 事件流
    # ------------------------------------------------------------------
    def push_hist(
        self, bot: str, uid: str, frm: str, to: str, by: str = "user", note: str = ""
    ) -> None:
        rows = self.hist.setdefault(str(bot), {}).setdefault(str(uid), [])
        rows.append(
            {
                "at": int(time.time()),
                "from": str(frm or ""),
                "to": str(to or ""),
                "by": str(by or "user"),
                "note": str(note or ""),
            }
        )
        if len(rows) > HIST_MAX:
            del rows[: len(rows) - HIST_MAX]

    def hist_of(self, bot: str, uid: str) -> List[Dict[str, Any]]:
        return list(self.hist.get(str(bot), {}).get(str(uid), []))

    def push_event(self, kind: str, bot: str, uid: str, text: str) -> None:
        self.events.append(
            {
                "at": int(time.time()),
                "kind": str(kind),
                "bot": str(bot),
                "uid": str(uid),
                "text": str(text),
            }
        )
        if len(self.events) > EVENT_MAX:
            del self.events[: len(self.events) - EVENT_MAX]

    def recent_events(self, limit: int = 40) -> List[Dict[str, Any]]:
        return list(reversed(self.events[-max(1, limit) :]))

    # ------------------------------------------------------------------
    # 群 / 私聊分离
    # ------------------------------------------------------------------
    def group_get(self, bot: str, umo: str, uid: str) -> str:
        return self.group_rel.get(str(bot), {}).get(str(umo), {}).get(str(uid), "")

    def group_set(self, bot: str, umo: str, uid: str, relation: str) -> None:
        users = self.group_rel.setdefault(str(bot), {}).setdefault(str(umo), {})
        if relation:
            users[str(uid)] = str(relation)
        else:
            users.pop(str(uid), None)
            if not users:
                self.group_rel.get(str(bot), {}).pop(str(umo), None)

    # ------------------------------------------------------------------
    # 关系演进
    # ------------------------------------------------------------------
    def affection(
        self,
        bot: str,
        uid: str,
        relation: str,
        category: str = "",
        now: Optional[float] = None,
    ) -> int:
        """好感度 = 类别基础分 + 熟悉度加成。没有统计的人直接拿基础分。"""
        lo, hi = category_range(category)
        if not relation:
            return 0
        count = self.count_of(bot, uid)
        days = self.days_since_first(bot, uid, now)
        fam = familiarity(count, days)
        return int(round(lo + (hi - lo) * fam))

    def phase(self, bot: str, uid: str, relation: str, category: str = "") -> str:
        return phase_of(self.affection(bot, uid, relation, category))

    # ------------------------------------------------------------------
    # 有效期
    # ------------------------------------------------------------------
    def expired(
        self, bot: str, uid: str, ttl_days: int, now: Optional[float] = None
    ) -> bool:
        """关系是否过期。ttl_days<=0 表示永久，永远不过期。"""
        if ttl_days <= 0:
            return False
        last = _as_int(self.stat_of(bot, uid).get("last"))
        if not last:
            entry_at = _as_int(self.plugin.meta_of(bot, uid).get("at"))
            last = entry_at
        if not last:
            return False
        now = now if now is not None else time.time()
        return (now - last) > ttl_days * 86400

    # ------------------------------------------------------------------
    # 限流
    # ------------------------------------------------------------------
    def rate_peek(
        self, key: str, limit: int, window: float = 60.0, now: Optional[float] = None
    ) -> bool:
        """只看还能不能用，不计数。用来看帮助、查列表这种不该占额度的动作。"""
        if limit <= 0:
            return True
        now = now if now is not None else time.time()
        bucket = [t for t in self._rate.get(key, []) if now - t < window]
        self._rate[key] = bucket
        return len(bucket) < limit

    def rate_take(
        self, key: str, limit: int, window: float = 60.0, now: Optional[float] = None
    ) -> bool:
        """检查并占一次额度。真正要改东西时才调。limit<=0 表示不限。"""
        if limit <= 0:
            return True
        if not self.rate_peek(key, limit, window, now):
            return False
        self._rate.setdefault(key, []).append(
            now if now is not None else time.time()
        )
        return True

    def rate_ok(
        self, key: str, limit: int, window: float = 60.0, now: Optional[float] = None
    ) -> bool:
        """兼容旧调用：检查并计数。"""
        return self.rate_take(key, limit, window, now)

    def rate_reset(self) -> None:
        self._rate.clear()


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
