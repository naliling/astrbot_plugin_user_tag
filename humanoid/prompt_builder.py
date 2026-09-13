"""把身体状态编译成模型能「感觉到」的上下文。

v2.13.x 的做法是把状态拼成一行标签卡（`精力状态一般；情绪调皮；社交能量低`）塞进
用户消息，模型看到的是一堆**数据**，于是只能再补一句「禁止提及任何具体数据」去堵它
的复述冲动。这一版改成三段：

1. **体感**：第一人称的生理感受短句，由 soma 的轴 + 显著度门控产生。多数轴多数时候
   不出现——真人也不会每条消息都报告自己的状态。
2. **说话形式**：身体对这一轮的硬约束建议（期望长度、提问倾向、适不适合长回复）。
   这比「语气慵懒」有效得多，因为它改变的是形式而不是措辞风格。
3. **场景与关系**：群聊还是私聊、时间地点天气、怎么称呼对方、对他的情绪标签。

三档注入 `full / low / mood_only` 都真正区分开；v2.13.2 里 `mood_only` 与 `low` 走
的是同一分支，`enable_chat_awareness`、`show_city_time_in_low_intrusion`、
`night_mode_force_sleep`、`last_interaction_mode` 四个配置项则完全没有代码读。
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig
from .data.mood_map import get_mood_label

_CJK_RANGES = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# 注入块的硬长度上限（字符）。换算成 token 大约 low 350 / full 700，
# 每次聊天请求都追加这一条，所以它必须是个定值而不是「看拼出来多少」。
INJECT_MAX_CHARS = {"low": 520, "full": 900, "mood_only": 260}


def estimate_tokens(text: str) -> int:
    """估算 token 数：中日韩一字算一 token，其余按 3.5 字符一 token。

    与自主拟人社交侧同口径；没装分词器时宁可高估也不能低估预算。
    """
    if not text:
        return 0
    cjk = len(_CJK_RANGES.findall(text))
    return int(math.ceil(cjk + (len(text) - cjk) / 3.5))

# 情绪标签 → 语气。v2.13.2 这份表放在已死的 humanoid/prompt.py 里，没有任何调用方。
MOOD_TONE_HINTS = {
    "亲密": "语气温柔，带一点亲昵",
    "依恋": "语气柔和，略带撒娇",
    "信赖": "语气坚定，充满信任",
    "热情": "语气活泼，表现出兴趣",
    "友好": "语气友善，保持礼貌",
    "平常": "语气自然，不刻意",
    "疏远": "语气客气，保持距离",
    "冷淡": "语气平淡，不热情",
    "敌视": "语气冷硬，保持警惕",
    "警惕": "语气谨慎，观察为主",
}

BOUNDARY_LINE = "这些是你自己的身体和状态，不是需要报告的数据；回复里不要出现数值、百分比或状态清单。"

# 低注入档下最多给几条体感：真人多数时候不觉得自己在报备身体。
MAX_FEELINGS_LOW = 2
MAX_FEELINGS_FULL = 5
FEELING_THRESHOLD_LOW = 0.55

EVENT_TEXT = {
    "conversation_started": "刚开始交流",
    "conversation_resumed": "对方刚重新接上对话（约离开5～30分钟）",
    "user_returned": {
        "medium_return": "对方隔了约30分钟～2小时重新出现",
        "long_return": "对方隔了约2～6小时重新出现",
        "short_return": "对方刚回来",
    },
    "long_gap": "对方隔了6小时以上重新出现",
}

AGENCY_LABELS = {
    "initiative": "主动",
    "curiosity": "好奇",
    "care": "关心",
    "social_willingness": "社交意愿",
    "continuation": "延续话题",
}


class PromptBuilder:
    def __init__(self, core_instance: "HumanoidCoreInstance") -> None:
        self._core = core_instance

    @property
    def config(self) -> HumanoidConfig:
        return self._core.config

    # ------------------------------------------------------------------

    def build(
        self,
        user_id: str,
        is_group: bool = False,
        events: Optional[List[Dict[str, Any]]] = None,
        agency: Optional[Dict[str, float]] = None,
    ) -> str:
        cfg = self.config
        events = events or []
        agency = agency or {}
        mode = cfg.inject_activity_context

        if mode == "mood_only":
            parts = []
            relation = self._block("关系", self._relation_lines(user_id, is_group, detailed=False))
            if relation:
                parts.append(relation)
            feelings = self._block("感觉", self._feelings_lines(max_items=1, threshold=0.7))
            if feelings:
                parts.append(feelings)
            return self._finish("\n".join(parts), cfg)

        if mode == "full":
            return self._finish(self._build_full(user_id, is_group, events, agency), cfg)

        return self._finish(self._build_low(user_id, is_group, events, agency), cfg)

    # ------------------------------------------------------------------
    # 分块
    # ------------------------------------------------------------------

    def _block(self, title: str, lines: List[str]) -> str:
        lines = [line for line in lines if line]
        if not lines:
            return ""
        return f"【{title}】" + "；".join(lines) + "。"

    def _feelings_lines(self, max_items: int, threshold: float) -> List[str]:
        """按显著度挑体感。低于门槛的一律不注入。"""
        core = self._core
        if not self.config.soma_enabled:
            return []
        try:
            feelings = core.soma.feelings(float(core.energy.energy))
        except Exception:
            return []
        picked = [item for item in feelings if item[0] >= threshold]
        picked.sort(key=lambda item: item[0], reverse=True)
        return [text for _, text in picked[:max_items]]

    def _form_lines(self) -> List[str]:
        core = self._core
        cfg = self.config
        if not cfg.soma_enabled:
            return []
        try:
            policy = core.soma.form_policy(float(core.energy.energy), float(core.social.value))
        except Exception:
            return []
        lines: List[str] = []
        max_chars = int(policy.get("max_chars", 120))
        if max_chars <= 24:
            lines.append(f"这一轮只说一两句，控制在{max_chars}字上下")
        elif max_chars <= 60:
            lines.append(f"话说得短，别超过{max_chars}字")
        elif not policy.get("long_reply_ok", True):
            lines.append("别展开成长篇")
        question_bias = float(policy.get("question_bias", 0.35))
        if question_bias <= 0.1:
            lines.append("这一轮不追问，先接住对方说的")
        elif question_bias >= 0.5:
            lines.append("可以自然地问一句")
        if policy.get("burst_ok"):
            lines.append("想说的东西可以分成几条短消息发")
        return lines

    def _night_lines(self) -> List[str]:
        """夜间/睡眠：真正按 night_mode_force_sleep 分强弱。"""
        cfg = self.config
        core = self._core
        if not cfg.night_mode_enabled or not core.clock.is_night():
            return []
        asleep = False
        if cfg.soma_enabled:
            try:
                asleep = core.soma.snapshot().get("asleep", 0.0) >= 1.0
            except Exception:
                asleep = False
        return build_night_lines(cfg, core.clock.is_deep_sleep(), asleep)

    def _relation_lines(self, user_id: str, is_group: bool, detailed: bool) -> List[str]:
        cfg = self.config
        core = self._core
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return []
        lines: List[str] = []
        try:
            data = core.mood.profile(user_id)
            label = get_mood_label(data["affection"], data["libido"], data["aggression"])
        except Exception:
            return []
        if detailed:
            lines.append(
                f"当前情绪数值：好感{float(data['affection']):.1f}/100，"
                f"亲近{float(data['libido']):.1f}/50"
            )
        hint = MOOD_TONE_HINTS.get(label)
        lines.append(f"对TA的感觉：{label}" + (f"（{hint}）" if hint else ""))
        tag = core.mood.tag(user_id)
        if tag and cfg.mood_tag_enabled:
            lines.append(f"心情标签：{tag}")
        return lines

    def _nickname_line(self, user_id: str, is_group: bool) -> str:
        cfg = self.config
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return ""
        nickname = self._core.mood.nickname(user_id)
        return f"你管TA叫{nickname}" if nickname else ""

    def _scene_lines(self, snap: Dict[str, Any], is_group: bool) -> List[str]:
        cfg = self.config
        lines: List[str] = []
        if cfg.enable_chat_awareness:
            lines.append("群聊里，周围还有人看着" if is_group else "只有你和TA两个人在聊")
        now = self._core.clock.now()
        lines.append(f"{snap['today']} {self._time_of_day(now.hour)}")
        if cfg.show_city_time_in_low_intrusion:
            lines.append(f"你在{snap['city']}")
        weather = snap.get("weather") or {}
        temp = _short_weather(weather)
        if temp:
            lines.append(temp)
        return lines

    def _state_lines(self, snap: Dict[str, Any], detailed: bool) -> List[str]:
        """传统状态行。

        开了生理层之后，low 档不再注入「精力状态良好」这类标签：体感已经把它说得更准，
        同时出现两份只会让模型去调和问题。关掉 soma 时仍需要它兜底。
        """
        lines: List[str] = []
        if detailed or not self.config.soma_enabled:
            lines.append(f"精力{snap['energy']['text']}")
        cycle = str(snap.get("cycle") or "").strip()
        if cycle and detailed:
            lines.append(cycle)
        proc = self._core.process.current()
        name = str(proc.get("name", "")).strip()
        phase = str(proc.get("phase", "")).strip()
        if name and name not in {"休息", "自由活动"}:
            lines.append(f"手上在做的：{name}/{phase}" if phase and phase != name else f"手上在做的：{name}")
        return lines

    def _behavior_lines(
        self, events: List[Dict[str, Any]], agency: Dict[str, float], with_previous: bool
    ) -> List[str]:
        if not events:
            return []
        top = events[0]
        event_type = str(top.get("type", ""))
        data = top.get("data") or {}
        mapped = EVENT_TEXT.get(event_type, "")
        if isinstance(mapped, dict):
            event_text = mapped.get(str(data.get("gap_bucket", "")), "对方隔了一段时间重新出现")
        else:
            event_text = mapped or "最近出现了交流变化"
        lines = [event_text]
        previous = data.get("previous_message") if with_previous else None
        if previous:
            previous = str(previous).replace("\n", " ").strip()[:60]
            if previous:
                lines.append(f"TA离开前说的是「{previous}」")
        if agency:
            strong = [
                AGENCY_LABELS[key]
                for key, value in sorted(agency.items(), key=lambda item: float(item[1]), reverse=True)
                if key in AGENCY_LABELS and float(value) >= 0.62
            ]
            if strong:
                lines.append("更" + "、更".join(strong[:2]))
        return lines

    # ------------------------------------------------------------------
    # 两档组装
    # ------------------------------------------------------------------

    def _build_low(self, user_id: str, is_group: bool, events, agency) -> str:
        snap = self._core.snapshot(refresh=False)
        parts: List[str] = [self._block("此刻", self._scene_lines(snap, is_group))]

        feelings = self._feelings_lines(MAX_FEELINGS_LOW, FEELING_THRESHOLD_LOW)
        night = self._night_lines()
        body = feelings + night
        if body:
            parts.append(self._block("我的感觉", body))

        form = self._form_lines()
        if form:
            parts.append(self._block("这一轮", form))

        relation = self._relation_lines(user_id, is_group, detailed=False)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            parts.append(self._block("对TA", relation))

        situ = self._behavior_lines(
            events, agency, with_previous=self.config.last_interaction_mode == "with_last_msg"
        )
        if situ:
            parts.append(
                self._block("刚刚", situ + ["这个背景参考一次就好，别反复追问同件事"])
            )

        hands = self._state_lines(snap, detailed=False)
        if hands:
            parts.append(self._block("身边", hands))

        return "\n".join(part for part in parts if part)

    def _build_full(self, user_id: str, is_group: bool, events, agency) -> str:
        snap = self._core.snapshot(refresh=False)
        lines: List[str] = []
        lines += self._scene_lines(snap, is_group)
        lines += self._state_lines(snap, detailed=True)
        lines += [f"社交能量{int(float(snap['social_energy']['value']))}%" if snap.get("social_energy") else ""]
        lines += self._feelings_lines(MAX_FEELINGS_FULL, 0.0)
        lines += self._night_lines()
        lines += self._form_lines()
        lines += self._relation_lines(user_id, is_group, detailed=True)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            lines.append(nickname)
        lines += self._behavior_lines(
            events, agency, with_previous=self.config.last_interaction_mode == "with_last_msg"
        )
        recent = self._core.process.recent()
        if recent:
            lines.append("最近做过：" + "、".join(recent))
        return ("【拟人状态】" + "；".join(line for line in lines if line) + "。")

    def _finish(self, text: str, cfg: HumanoidConfig) -> str:
        if not text:
            return ""
        full = text + "\n" + BOUNDARY_LINE
        limit = INJECT_MAX_CHARS.get(cfg.inject_activity_context, 520)
        if len(full) > limit:
            # 按块切而不是按字硬截：截到半句上模型会自己补下去。
            kept: List[str] = []
            used = 0
            for line in full.split("\n"):
                cost = len(line) + 1
                if used + cost > limit:
                    break
                kept.append(line)
                used += cost
            kept.append("（以上状态只供你参考，不必逐条回应。）")
            full = "\n".join(kept)
        return full

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 8:
            return "清晨"
        if 8 <= hour < 12:
            return "上午"
        if 12 <= hour < 14:
            return "中午"
        if 14 <= hour < 18:
            return "下午"
        if 18 <= hour < 21:
            return "傍晚"
        if 21 <= hour < 24:
            return "晚上"
        return "深夜"


def build_night_lines(cfg: HumanoidConfig, is_deep: bool, asleep: bool) -> List[str]:
    """夜间语气：纯函数，调用方负责保证现在确实落在夜间窗口里。

    `night_mode_force_sleep` 必须是「更严格」的那一档：开着时直接要求只回一句要休息，
    关着时只是把话说短。插件拦不住回复，所以不写「不应回复」这类模型无法执行的禁令。
    """
    if cfg.night_mode_force_sleep:
        if is_deep or asleep:
            return ["你在睡觉，被吵醒就回一句「我现在需要休息，明天再聊吧」，不要接着聊"]
        return ["夜已深，简短回应并提一句想睡了"]
    if is_deep or asleep:
        return ["刚被吵醒，迷迷糊糊、句子断续，说不长"]
    return ["夜里慵懒，说话轻、短"]


def _short_weather(weather: Dict[str, Any]) -> str:
    """天气只留一句能用的；没配好时直接不注入，而不是把配置说明书念给模型听。"""
    env = str(weather.get("env", "")).strip()
    if not env:
        return ""
    if any(word in env for word in ("未填", "未开启", "获取中", "没配天气")):
        return ""
    return env.replace("当前城市", "这边")[:26]
