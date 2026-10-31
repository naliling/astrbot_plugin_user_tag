"""把 user_tag 的关系数据导出成 autonomous_social 认的 state.json。

方向是单向的：user_tag 写，AS 只读。AS 的 social/core_bridge.py 认死两个路径
（plugin_data/humanoid_core/state.json 与 plugin_data/astrbot_plugin_humanoid_core/state.json），
没有推送入口，所以要「主动找 AS」只能由这边把文件按它的契约摆好。

三条硬规矩（照抄 core_bridge 的实现，不是猜的）：
1. schema 必须正好是 humanoid.contract/{整数}；
2. generated_at 超过 45 分钟，AS 会把内容当过期清空 —— 所以要心跳；
3. 文件里已经有别人（Humanoid Core 本体）写的、且还新鲜，就让位，不抢。

导出默认关闭。抢文件的代价远大于晚一点接上。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .relcore import relation_category_of

SCHEMA_VERSION = 1
SCHEMA_PREFIX = "humanoid.contract/"
CONTRACT_STALE_SECONDS = 45 * 60
CONTRACT_EXPECTED_CAPABILITIES: List[str] = [
    "day",
    "ongoing",
    "routine",
    "wake",
    "event",
    "identity",
    "outfit",
]
WRITER_MARK = "astrbot_plugin_user_tag"

# 插件自报的路径（相对 plugin_data/）。优先用 Humanoid Core 本体的标准目录名，
# AS 两个都认，但这个才是 HC 用 StarTools.get_data_dir 拿到的那个。
#: autonomous_social 的读取顺序（core_bridge._STATE_FILENAMES，取第一个存在的）。
#: 注意它把人形核心本体那个目录排在前面 —— 本插件默认写的是第二个，
#: 所以 HC 在跑的时候，我们写的文件它根本不会读。
AS_READ_ORDER: Tuple[str, ...] = (
    "humanoid_core/state.json",
    "astrbot_plugin_humanoid_core/state.json",
)

#: 本插件默认写到哪个文件。挑第二个的理由：第一个是 Humanoid Core 本体的地盘，
#: 抢它等于把对方 5000 多人的状态覆盖掉。宁可写个「没人读」的文件，也不砸别人。
DEFAULT_TARGET = "astrbot_plugin_humanoid_core/state.json"
ALT_TARGET = "humanoid_core/state.json"

# 关系类别 -> 好感度基础值（只用于 base_affection 这个字段，主分数在 relcore 里算）
_BASE_AFFECTION: Dict[str, int] = {
    "日常": 30,
    "亲友": 60,
    "恋爱": 80,
    "心动": 65,
    "恋爱设定": 82,
    "身份": 35,
    "主仆": 50,
    "奇幻": 55,
    "玩梗": 45,
}


def _fields_hash(payload: Dict[str, Any]) -> str:
    """照 core_bridge._fields_hash 的口径算字段指纹。

    遍历整个 dict 的键路径（按 key 排序），用换行拼起来取 sha256 前 16 位。
    算的时候必须把 fields_hash 自己排除掉，否则永远算不出稳定值。
    """
    paths: List[str] = []

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for key in node:
                path = f"{prefix}.{key}" if prefix else str(key)
                if path == "fields_hash":
                    continue
                paths.append(path)
                walk(node[key], path)
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix)

    walk(payload, "")
    joined = "\n".join(sorted(paths))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class CoreStateExporter:
    """组装 + 原子写 + 冲突检测 + 心跳。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.last_written_at: int = 0
        self.last_status: str = "未导出"
        self.last_error: str = ""
        self.skipped_reason: str = ""

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def plugin_data_dir(self) -> Path:
        return self.plugin.data_file.parent.parent  # .../plugin_data

    def target_path(self) -> Path:
        raw = str(self.plugin.config.get("export_target_path", "") or "").strip()
        rel = raw.lstrip("/\\") or DEFAULT_TARGET
        return self.plugin_data_dir() / rel

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------
    def build(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = int(now if now is not None else time.time())
        roles: Dict[str, Any] = {}
        for bot in self.plugin.bots():
            users: Dict[str, Any] = {}
            for uid in self.plugin.data.get(bot, {}) or {}:
                row = self._user_row(bot, uid, now)
                if row:
                    users[uid] = row
            if not users:
                continue
            roles[bot] = {
                "self": {"contract": self._self_contract(now)},
                "users": users,
            }

        payload: Dict[str, Any] = {
            "schema": f"{SCHEMA_PREFIX}{SCHEMA_VERSION}",
            "generated_at": now,
            "writer": WRITER_MARK,
            "roles": roles,
        }
        payload["fields_hash"] = _fields_hash(payload)
        return payload

    def _self_contract(self, now: int) -> Dict[str, Any]:
        """本体状态这一块 user_tag 没有话语权，给一组中性值把契约撑住即可。

        真正的心跳、睡眠、饥饿归 Humanoid Core 管；这边只要保证 AS 读到的是
        「形状对、没过期」的文件，users 里的关系数据才会被采用。
        """
        return {
            "schema": f"{SCHEMA_PREFIX}{SCHEMA_VERSION}",
            "generated_at": now,
            "capabilities": list(CONTRACT_EXPECTED_CAPABILITIES),
            "body": {
                "sleep_pressure": 0.2,
                "sleep_debt": 0.1,
                "hunger": 0.3,
                "discomfort": 0.1,
                "arousal": 0.2,
                "social_desire": 0.5,
                "last_sleep_hours": 7.5,
                "energy": 0.7,
                "asleep": False,
            },
        }

    def _user_row(self, bot: str, uid: str, now: int) -> Optional[Dict[str, Any]]:
        relation = str(self.plugin.data.get(bot, {}).get(uid, "") or "")
        if not relation:
            return None
        core = self.plugin.core
        category = relation_category_of(self.plugin, relation)
        note = self.plugin.note_of(bot, uid)
        try:
            affection = core.affection(bot, uid, relation, category)
        except Exception:
            affection = _BASE_AFFECTION.get(category, 40)
        stat = core.stat_of(bot, uid) or {}
        nickname = note if note and self._looks_like_name(note) else uid
        return {
            "nickname": nickname,
            "display_name": nickname,
            "name": nickname,
            "nickname_src": WRITER_MARK,
            "relation": relation,
            "relation_category": category,
            "mood": {"affection": affection},
            "base_affection": _BASE_AFFECTION.get(category, 40),
            "first_met": int(stat.get("first") or 0),
            "last_interaction": int(stat.get("last") or 0),
            "attention": 0.5,
            "mood_tag": core.phase(bot, uid, relation, category)
            if hasattr(core, "phase")
            else "",
            "said": [],
        }

    @staticmethod
    def _looks_like_name(text: str) -> bool:
        text = str(text or "").strip()
        return (
            bool(text)
            and len(text) <= 12
            and not any(ch in text for ch in "。！？，,.;；\n")
        )

    # ------------------------------------------------------------------
    # 写盘
    # ------------------------------------------------------------------
    def write(self, force: bool = False) -> bool:
        path = self.target_path()
        # 硬红线：humanoid_core/state.json 是 Humanoid Core 本体的全量状态（5000+ 人、
        # 两兆多）。这个插件只负责写自己的关系快照，不能为了「让对方读到」就把别人的
        # 状态覆盖掉。配置写错也拦：宁可导不出去，不砸别人。
        if os.path.normcase(str(path)) == os.path.normcase(
            str(self.plugin_data_dir() / AS_READ_ORDER[0])
        ):
            self.last_status = "拒绝"
            self.skipped_reason = (
                "目标是 Humanoid Core 本体的 state.json，本插件不覆盖它"
            )
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.last_status = "失败"
            self.last_error = f"建目录失败：{exc}"
            return False

        if not force:
            reason = self._conflict_reason(path)
            if reason:
                self.skipped_reason = reason
                self.last_status = "让位"
                return False

        payload = self.build()
        tmp = path.with_name(path.name + ".ut.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as exc:
            self.last_status = "失败"
            self.last_error = str(exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

        self.last_written_at = int(time.time())
        self.last_status = "已写入"
        self.last_error = ""
        self.skipped_reason = ""
        return True

    def _conflict_reason(self, path: Path) -> str:
        """返回不让位的理由；空串表示可以写。"""
        if not path.exists():
            return ""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return ""  # 读不动或坏了，直接接管
        if not isinstance(raw, dict):
            return ""
        if str(raw.get("writer") or "") == WRITER_MARK:
            return ""  # 是我们自己上次写的
        schema = str(raw.get("schema") or "")
        if not schema.startswith(SCHEMA_PREFIX):
            return ""  # 不是契约文件，接管
        try:
            generated = int(raw.get("generated_at") or 0)
        except (TypeError, ValueError):
            generated = 0
        age = time.time() - generated
        if generated and age < CONTRACT_STALE_SECONDS:
            return (
                f"目标文件 {path.name} 由其他插件在 {int(age // 60)} 分钟前写入，"
                "仍然新鲜，已让位。"
            )
        return ""  # 过期了，接管

    def preview(self) -> Dict[str, Any]:
        return self.build()

    def read_path(self) -> Path:
        """autonomous_social 实际会读的那个文件。

        core_bridge._resolve_path() 的逻辑很硬：按 _STATE_FILENAMES 的顺序取**第一个
        存在的**。所以只要 Humanoid Core 本体在写 humanoid_core/state.json，
        这边写 astrbot_plugin_humanoid_core/state.json 就是写了个没人看的文件。
        这不是本插件的 bug，是对方的选择顺序——但必须在面板上说出来，
        否则用户会以为导出已经生效。
        """
        base = self.plugin_data_dir()
        for rel in AS_READ_ORDER:
            candidate = base / rel
            if candidate.exists():
                return candidate
        return base / AS_READ_ORDER[0]

    def status(self) -> Dict[str, Any]:
        path = self.target_path()
        reader = self.read_path()
        effective = os.path.normcase(str(reader)) == os.path.normcase(str(path))
        occupied_by = ""
        if not effective:
            occupied_by = "Humanoid Core 本体" if reader.name == "state.json" else "其他插件"
        return {
            "enabled": bool(self.plugin.config.get("export_to_social", False)),
            "path": str(path),
            "exists": path.exists(),
            "read_path": str(reader),
            "effective": effective,
            "occupied_by": occupied_by,
            "last_written_at": self.last_written_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "skipped_reason": self.skipped_reason,
            "stale_seconds": CONTRACT_STALE_SECONDS,
            "interval_min": int(self.plugin.config.get("export_interval_min", 20) or 0),
        }
