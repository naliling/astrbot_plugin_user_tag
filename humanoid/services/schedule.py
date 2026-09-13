"""日程服务 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from ..clock import Clock
from ..config import HumanoidConfig
from ..data.schedule_templates import get_fallback_schedule
from ..jsonx import extract_json_array
from ..llm import LLMGateway, LLMResult, ProviderResolver
from ..role_scope import RoleScope
from ..slots import (
    DAY_MINUTES,
    Slot,
    coverage_is_complete,
    find_slot,
    format_time,
    is_sleep_event,
    normalize_slots,
    parse_time,
)

PURPOSE = "日程生成"
SOURCE_TEMPLATE = "template"
SOURCE_LLM = "llm"
MIN_RETRY_BACKOFF_SECONDS = 60.0


SLEEP_EVENT = "睡眠"
WAKE_SIDE_EVENT = "赖床与洗漱"     # 起床点后、日程还写着睡的那截零头
BEDSIDE_EVENT = "夜间洗漱"         # 入睡点前、日程已写着睡的那截零头


def routine_prompt(cfg: HumanoidConfig) -> str:
    """写进日程 prompt 的作息硬约束。

    不加这个约束时，模型会按自己的直觉把她排成 00:00–08:00 睡觉，而夜间窗口写着
    5 点结束——于是早上永远处于「日程在睡、生物钟已醒」的两套时间里。
    """
    if not cfg.night_mode_enabled or not cfg.schedule_follow_night_window:
        return ""
    start, end = cfg.night_start_hour, cfg.night_end_hour
    if start == end:
        return ""
    span = cfg.night_span_hours
    lines = [
        f"7. 她的作息必须遵守：{start:02d}:00 上床，生物钟夜到 {end:02d}:00 结束。",
        f"   睡眠排成首尾相接的两段：{start:02d}:00→24:00 与 00:00→{end:02d}:00，"
        "这两段的 event 里要带「睡眠」二字；",
        f"   {end:02d}:00 往后从起床、洗漱开始排。",
    ]
    if cfg.sleep_need_hours > span + 0.5:
        lines.append(
            f"   她一晚需要睡 {cfg.sleep_need_hours:g} 小时，比这个窗口更长，"
            "所以早上会赖床、不太起得来。"
        )
    return "\n".join(lines) + "\n"


def _intersect(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int] | None:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if hi > lo else None


def _subtract(seg: tuple[int, int], spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    parts = [seg]
    for span in spans:
        nxt: list[tuple[int, int]] = []
        for part in parts:
            overlap = _intersect(part, span)
            if overlap is None:
                nxt.append(part)
                continue
            if part[0] < overlap[0]:
                nxt.append((part[0], overlap[0]))
            if overlap[1] < part[1]:
                nxt.append((overlap[1], part[1]))
        parts = nxt
    return parts


def align_sleep_to_night(slots: list[Slot], cfg: HumanoidConfig) -> list[Slot]:
    """把一份日程里的睡眠区间挪到夜间窗口上。

    内置模板的睡眠时间是写死的（00:00–07:30 那一类），而用户设的夜间窗口是另一回事；
    不对齐的话「她几点起」就有两个答案。这里按夜间窗口把每个时段切成「睡」与
    「不睡」两部分，剩下的交给 normalize_slots 保证首尾相连。
    """
    if not cfg.night_mode_enabled or not cfg.schedule_follow_night_window:
        return slots
    start, end = cfg.night_start_hour * 60, cfg.night_end_hour * 60
    if start == end:
        return slots
    spans = [(start, DAY_MINUTES), (0, end)] if start > end else [(start, end)]

    pieces: list[Slot] = []
    for slot in slots:
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        if lo is None or hi is None or hi <= lo:
            pieces.append(slot)
            continue
        was_sleep = is_sleep_event(slot.get("event"))
        covered = [span for span in spans if _intersect((lo, hi), span) is not None]
        if not covered:
            # 完全落在夜间窗口外：原样保留。午休就是午休，不该被改名。
            pieces.append(dict(slot))
            continue
        rest = _subtract((lo, hi), covered)
        if not rest:
            # 整段都在窗口内，没有被切开，事件名也不用动。
            pieces.append(_sleep_piece((lo, hi), slot, was_sleep))
            continue
        for span in covered:
            overlap = _intersect((lo, hi), span)
            if overlap is not None:
                pieces.append(_sleep_piece(overlap, slot, was_sleep))
        for part in rest:
            pieces.append(_awake_piece(part, slot, was_sleep, start, end))

    aligned = normalize_slots(
        pieces,
        max_slots=max(8, cfg.schedule_max_slots),
        align_minutes=cfg.granularity_minutes,
    )
    return aligned or slots


def _sleep_piece(seg: tuple[int, int], slot: Slot, was_sleep: bool) -> Slot:
    piece = dict(slot)
    piece["start"], piece["end"] = format_time(seg[0]), format_time(seg[1])
    if not was_sleep:
        # 本来不是睡眠的时段落进了夜间窗口：改成真睡，否则身体不会把它当觉。
        piece["event"] = SLEEP_EVENT
        piece["location"] = "卧室"
        piece["emotion"] = "沉睡"
        piece["energy_rate"] = 0.15
    return piece


def _awake_piece(
    seg: tuple[int, int],
    slot: Slot,
    was_sleep: bool,
    night_start: int,
    night_end: int,
) -> Slot:
    piece = dict(slot)
    piece["start"], piece["end"] = format_time(seg[0]), format_time(seg[1])
    if was_sleep:
        # 从睡眠里切出来的零头：名字里不能再带「睡」，否则身体会把它当成还在睡。
        # 紧贴起床点之后的是赖床，紧贴入睡点之前的是睡前洗漱，两者不是一回事。
        if seg[0] == night_end:
            piece["event"] = WAKE_SIDE_EVENT
            piece["emotion"] = "慢慢清醒"
        elif seg[1] == night_start:
            piece["event"] = BEDSIDE_EVENT
            piece["emotion"] = "困倦"
        else:
            piece["event"] = WAKE_SIDE_EVENT
            piece["emotion"] = "慢慢清醒"
    return piece


def sleep_spans(slots: list[Slot]) -> list[Slot]:
    """日程里算「她在睡」的时段（与身体层同一套判定）。"""
    return [slot for slot in slots if is_sleep_event(slot.get("event"))]


def schedule_wake_minute(slots: list[Slot]) -> int | None:
    """从日程里读出她的起床时间：凌晨那段睡眠的结束点。"""
    for slot in sleep_spans(slots):
        if parse_time(slot.get("start")) == 0:
            end = parse_time(slot.get("end"))
            if end:
                return end
    return None


def schedule_wake_text(slots: list[Slot]) -> str:
    minute = schedule_wake_minute(slots)
    return format_time(minute) if minute is not None else ""


def build_prompt(cfg: HumanoidConfig, today: str, weekday: str) -> str:
    max_slots = cfg.schedule_max_slots
    min_slots = max(4, min(max_slots, max_slots // 2))
    step = cfg.granularity_minutes
    if step > 1:
        align_hint = f"所有 start / end 必须对齐到 {step} 分钟的整数倍。"
    else:
        align_hint = "时间点可以自然决定，不必对齐。"

    extra = cfg.schedule_prompt_extra.strip()
    extra_line = f"额外偏好：{extra}\n" if extra else ""

    return (
        f"请为「{cfg.character_personality}」这个人设，规划今天一整天的生活日程。\n"
        f"今天是 {today}，星期{weekday}。\n"
        f"{extra_line}"
        "\n输出要求：\n"
        "1. 只输出一个 JSON 数组，不要 Markdown 代码块。\n"
        "2. 每个元素：{\"start\": \"00:00\", \"end\": \"07:30\", \"event\": \"睡眠休息\", "
        "\"location\": \"卧室\", \"emotion\": \"平静\", \"energy_rate\": 0.15}\n"
        f"3. 总共输出 {min_slots}~{max_slots} 个时段，把连续同类活动合并。\n"
        "4. 时段必须首尾相连：00:00 开始，24:00 结束，不重叠。\n"
        f"5. {align_hint}\n"
        "6. energy_rate：睡眠/休息为正（0.05~0.2），工作/外出/社交为负（-0.05~-0.15）。\n"
        + routine_prompt(cfg)
    )


class ScheduleService:
    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock: Clock,
        spawn_fn=None,
        logger=None,
        monotonic: Callable[[], float] | None = None,
    ):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._log = logger
        self._spawn = spawn_fn
        self._generating = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last_error = ""
        self._retry_after = 0.0
        # 失败退避的计时源：必须可注入，否则测试无法验证「退避窗口内不再投递」。
        self._monotonic = monotonic or time.monotonic
        # 跨天时先保留上一份有效日程，等待新日程成功生成；绝不把持久化的 LLM 日程覆盖成模板。
        self._pending_date = ""
        self._pending_slots: list[Slot] | None = None

        self.resolver = None
        self.gateway = None
        # 新日程装上后的一次性回调：身体需要知道「睡眠区间换了」，不能把它当成真的醒了。
        self.on_install = None

    def set_resolver_gateway(self, resolver, gateway):
        self.resolver = resolver
        self.gateway = gateway

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    def current_slots(self) -> list[Slot]:
        """返回今天可用的日程。

        重要：跨天/升级时不能把上一份已生成日程直接覆盖成内置模板。
        模板只作为“等待今天 LLM 日程生成”的临时兜底，不写入持久状态。
        """
        today = self._clock.today_str()
        data = self._scope.self_state
        slots = data.get("daily_schedule")
        stored_date = str(data.get("today_date") or "")

        if stored_date == today and isinstance(slots, list) and slots:
            self._pending_date = ""
            self._pending_slots = None
            return slots

        if self._pending_date != today or not self._pending_slots:
            self._pending_date = today
            self._pending_slots = self._template_slots(today)

        return self._pending_slots

    def current_slot(self, minutes: int | None = None) -> Slot:
        if minutes is None:
            now = self._clock.now()
            minutes = now.hour * 60 + now.minute
        return find_slot(self.current_slots(), minutes)

    @property
    def source(self) -> str:
        return str(self._scope.get_self("schedule_source", SOURCE_TEMPLATE))

    @property
    def source_text(self) -> str:
        return "大模型生成" if self.source == SOURCE_LLM else "内置模板"

    @property
    def generating(self) -> bool:
        return self._generating

    @property
    def retry_after(self) -> float:
        return max(0.0, self._retry_after - self._monotonic())

    def _template_slots(self, today: str) -> list[Slot]:
        cfg = self.config
        raw = get_fallback_schedule(today)
        base = normalize_slots(raw, max_slots=max(8, cfg.schedule_max_slots)) or raw
        return align_sleep_to_night(base, cfg)

    def _install(self, slots: list[Slot], today: str, source: str) -> list[Slot]:
        # 模型不一定听约束（也常见它把睡眠写成 00:00–08:00），装上去之前再按夜间窗口切一次。
        slots = align_sleep_to_night(slots, self.config)
        self._scope.update_self(
            today_date=today,
            daily_schedule=slots,
            schedule_source=source,
            schedule_generated_at=self._clock.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        if self.on_install is not None:
            try:
                self.on_install()
            except Exception:
                pass
        if self._log and self.config.debug_mode:
            self._log.debug(f"[humanoid_core] 日程写入存储: source={source}, slots={len(slots)}")
        return slots

    def _today_changed(self, today: str) -> bool:
        """今天是否还没有一份属于今天的日程，且今天还没试过生成。

        不能只看 `today_date`：它只在生成成功时写入。若只看它，模型持续不可用（配错
        provider、超时）时 `today_date` 永远不是今天，于是每个后台周期都被当成「跨天」
        而绕过退避与冷却，变成每 30 秒一次永久重试。因此「今天试过但没有结果」不算跨天。
        """
        if str(self._scope.get_self("today_date", "") or "") == today:
            return False
        return str(self._scope.get_self("schedule_attempt_date", "") or "") != today

    def request_refresh(self, force: bool = False, ignore_cooldown: bool = False) -> bool:
        if self._task and not self._task.done():
            return False
        cfg = self.config
        if not cfg.use_llm_schedule:
            return False

        today = self._clock.today_str()
        date_changed = self._today_changed(today)
        # 新的一天必须尝试生成，即使上一天的 provider 失败冷却还没结束。
        effective_force = bool(force or date_changed)
        effective_ignore = bool(ignore_cooldown or date_changed)

        if not effective_force and self._scope.get_self("schedule_source") == SOURCE_LLM:
            return False
        if not effective_force and self.retry_after > 0:
            return False

        coro = self.ensure_fresh(force=effective_force, ignore_cooldown=effective_ignore)
        name = f"humanoid-schedule-refresh-{self._scope.role_id}"
        if self._spawn:
            self._task = self._spawn(coro, name)
        else:
            self._task = asyncio.create_task(coro, name=name)
        return True

    async def ensure_fresh(self, force: bool = False, ignore_cooldown: bool = False) -> bool:
        cfg = self.config
        if not cfg.use_llm_schedule:
            return False

        today = self._clock.today_str()
        if self._today_changed(today):
            force = True
            ignore_cooldown = True
        # 先把「今天试过」记下来：失败时 today_date 不会变，不记这一笔的话下一次调用
        # 仍然被当成跨天，退避窗口形同不存在。
        if str(self._scope.get_self("schedule_attempt_date", "") or "") != today:
            self._scope.set_self("schedule_attempt_date", today)

        if (
            not force
            and str(self._scope.get_self("today_date", "") or "") == today
            and self._scope.get_self("schedule_source") == SOURCE_LLM
        ):
            return False
        if not force and self.retry_after > 0:
            return False

        # 只在需要时创建临时模板，不落盘。
        self.current_slots()

        async with self._lock:
            stored_date = str(self._scope.get_self("today_date", "") or "")
            if not force and stored_date == today and self._scope.get_self("schedule_source") == SOURCE_LLM:
                return False
            return await self._generate(cfg, today, ignore_cooldown)

    async def _generate(self, cfg: HumanoidConfig, today: str, ignore_cooldown: bool) -> bool:
        self._generating = True
        try:
            ok = await self._generate_inner(cfg, today, ignore_cooldown)
        finally:
            self._generating = False
        if ok:
            self._retry_after = 0.0
        else:
            backoff = max(MIN_RETRY_BACKOFF_SECONDS, float(cfg.schedule_provider_cooldown_minutes) * 60)
            self._retry_after = self._monotonic() + backoff
        return ok

    async def _generate_inner(self, cfg: HumanoidConfig, today: str, ignore_cooldown: bool) -> bool:
        if self.gateway is None:
            self.last_error = "LLM Gateway 未初始化"
            return False

        prompt = build_prompt(cfg, today, self._clock.weekday())
        if self._log and cfg.debug_mode:
            self._log.debug(f"[humanoid_core] 日程生成提示词:\n{prompt}")

        result: LLMResult = await self.gateway.generate(
            prompt=prompt,
            chain=cfg.schedule_provider_ids,
            allow_global=cfg.schedule_allow_global_fallback,
            timeout=float(cfg.schedule_llm_timeout_seconds),
            attempts_per_provider=cfg.schedule_generation_max_attempts,
            retry_interval=float(cfg.schedule_retry_interval_seconds),
            purpose=PURPOSE,
            ignore_cooldown=ignore_cooldown,
        )
        if not result.ok:
            self.last_error = result.summary()
            if self._log and cfg.debug_mode:
                self._log.debug(f"[humanoid_core] 日程生成失败: {self.last_error}")
            return False

        parsed = extract_json_array(result.text)
        slots = (
            normalize_slots(parsed, max_slots=cfg.schedule_max_slots, align_minutes=cfg.granularity_minutes)
            if parsed is not None
            else None
        )
        if not slots or not coverage_is_complete(slots):
            self.last_error = f"无法解析日程：{result.text[:160]}"
            if self._log and cfg.debug_mode:
                self._log.debug(f"[humanoid_core] 日程解析失败: {self.last_error}")
            return False

        self._install(slots, today, SOURCE_LLM)
        self._pending_date = ""
        self._pending_slots = None
        self.last_error = ""
        if self._log:
            self._log.info(f"[humanoid_core] 日程生成成功，共 {len(slots)} 个时段")
            if cfg.debug_mode:
                self._log.debug(f"[humanoid_core] 新日程: {slots}")
        return True

    def status(self) -> dict:
        today = self._clock.today_str()
        pending = str(self._scope.get_self("today_date", "") or "") != today
        source = self.source
        source_text = self.source_text
        if pending and self.config.use_llm_schedule:
            source_text = "等待大模型生成（临时模板仅作过渡）"
        return {
            "date": today,
            "stored_date": self._scope.get_self("today_date", ""),
            "slots": len(self.current_slots()),
            "sleep_spans": [
                f"{s.get('start')}-{s.get('end')} {s.get('event')}" for s in sleep_spans(self.current_slots())
            ],
            "wake_at": schedule_wake_text(self.current_slots()),
            "source": source,
            "source_text": source_text,
            "generated_at": self._scope.get_self("schedule_generated_at", ""),
            "last_error": self.last_error,
            "generating": self.generating,
            "retry_after": self.retry_after,
            "pending_today": pending,
        }

    async def aclose(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass