"""`/拟人诊断` 的报告生成。"""

from __future__ import annotations

from typing import Any

from .config import HumanoidConfig
from .slots import parse_time
from .llm import (
    GLOBAL_LABEL,
    PURPOSE_MOOD as MOOD_PURPOSE,
    PURPOSE_SCHEDULE as SCHEDULE_PURPOSE,
    LLMGateway,
    ProviderResolver,
)

OK_MARK = "✓"
BAD_MARK = "✗"
WARN_MARK = "⚠"


def _routine_lines(
    cfg: HumanoidConfig,
    schedule_status: dict[str, Any],
    body_status: dict[str, Any] | None,
) -> list[str]:
    """作息：把「她几点起」这件事的三个参与方摊开说。"""
    lines: list[str] = []
    if not cfg.night_mode_enabled:
        lines.append("- 生物钟夜已关闭：不会攒睡眠债，也不会有夜间语气；没日程时她永远不算在睡")
    else:
        start, end = cfg.night_start_hour, cfg.night_end_hour
        span = cfg.night_span_hours
        lines.append(
            f"- 生物钟夜 {start:02d}:00 → {end:02d}:00（{span:g} 小时），"
            f"一晚需要 {cfg.sleep_need_hours:g} 小时"
        )
        if span and cfg.sleep_need_hours > span + 0.5:
            lines.append(
                f"  {WARN_MARK} 窗口比她需要的睡眠短 {cfg.sleep_need_hours - span:.1f} 小时："
                "一夜睡不够，早上会带着没清完的困意起床（想赖床是必然的）"
            )
        lines.append(
            f"- 日程贴合作息：{'开启（模型排的睡眠区间会被改到夜间窗口上）' if cfg.schedule_follow_night_window else '关闭（睡眠区间由模型自由发挥）'}"
        )

    wake = str(schedule_status.get("wake_at") or "")
    spans = schedule_status.get("sleep_spans") or []
    if wake:
        lines.append(f"- 今日日程里的起床时间：{wake}；睡眠段：{'、'.join(spans) or '无'}")
        if cfg.night_mode_enabled:
            minutes = parse_time(wake)
            if minutes is not None and abs(minutes - cfg.night_end_hour * 60) > 30:
                lines.append(
                    f"  {WARN_MARK} 与生物钟夜的结束点 {cfg.night_end_hour:02d}:00 不一致："
                    "身体以日程为准，她真的会睡到那个点"
                )
    elif spans:
        lines.append(f"- 今日日程里的睡眠段：{'、'.join(spans)}（凌晨没找到固定的起床点）")
    else:
        lines.append("- 今日日程里没有标「睡眠」的时段：她白天也不会算作在睡")

    snap = (body_status or {}).get("soma") or {}
    if snap:
        slept = float(snap.get("last_sleep_hours") or 0.0)
        if slept:
            gap = cfg.sleep_need_hours - slept
            tail = f"，比该睡的少 {gap:.1f} 小时" if gap > 0.5 else ""
            lines.append(f"- 昨夜实际睡了 {slept:.1f} 小时{tail}")
        lines.append(
            f"- 此刻：困意 {float(snap.get('sleep_pressure', 0)):.0f}/100、"
            f"睡眠债 {float(snap.get('sleep_debt', 0)):.1f}h、"
            f"{'正在睡' if snap.get('asleep') else '醒着'}"
        )
    return lines


def _resolve_line(
    resolver: ProviderResolver,
    label: str,
    provider_id: str,
    available: list[str],
) -> str:
    if not provider_id:
        return f"- {label}: 未配置"
    provider = resolver.resolve(provider_id)
    if provider is not None:
        actual = resolver.id_of(provider)
        suffix = "" if actual == provider_id else f"（实际匹配到 {actual}）"
        return f"- {label}: 「{provider_id}」{OK_MARK} 已解析{suffix}"
    near = [pid for pid in available if pid.strip().casefold() == provider_id.strip().casefold()]
    if near:
        hint = f"（注意大小写：可用列表里是 {near[0]}）"
    elif available:
        hint = "（可用列表里没有它）"
    else:
        hint = ""
    return f"- {label}: 「{provider_id}」{BAD_MARK} 未找到{hint}"


def _chain_pick(
    resolver: ProviderResolver,
    gateway: LLMGateway,
    chain: tuple[tuple[str, str], ...],
    allow_global: bool,
    purpose: str,
) -> str:
    for label, provider_id in chain:
        if gateway.cooldown_remaining(provider_id, purpose) > 0:
            continue
        if resolver.resolve(provider_id) is not None:
            return f"{label}({provider_id})"
    if allow_global:
        provider = resolver.resolve_global(None)
        if provider is not None:
            return f"{GLOBAL_LABEL}({resolver.id_of(provider)})"
    return "无可用模型 → 将使用内置日程模板"


def build_report(
    *,
    cfg: HumanoidConfig,
    resolver: ProviderResolver,
    gateway: LLMGateway,
    schedule_status: dict[str, Any],
    process_status: dict[str, Any],   # 新增参数
    version: str,
    body_status: dict[str, Any] | None = None,
    inject_estimate: dict[str, int] | None = None,
) -> str:
    available = resolver.available_ids()
    lines = [f"〖拟人诊断〗v{version}", "", "【AstrBot 可用对话模型 id】"]
    lines.append(f"  {available}" if available else "  （空 —— AstrBot 还没有配置任何对话模型）")

    lines += ["", "【日程模型链】"]
    lines.append(_resolve_line(resolver, "首选模型", cfg.schedule_provider_name, available))
    lines.append(_resolve_line(resolver, "备用模型", cfg.schedule_fallback_provider_name, available))

    if cfg.schedule_allow_global_fallback:
        provider = resolver.resolve_global(None)
        if provider is not None:
            lines.append(f"- 全局默认回退: 已开启 {OK_MARK} → {resolver.id_of(provider)}")
        else:
            lines.append(f"- 全局默认回退: 已开启，但 AstrBot 没设默认对话模型 {BAD_MARK}")
    else:
        lines.append("- 全局默认回退: 已关闭（schedule_allow_global_fallback = false）")

    picked = _chain_pick(
        resolver,
        gateway,
        cfg.schedule_provider_ids,
        cfg.schedule_allow_global_fallback,
        SCHEDULE_PURPOSE,
    )
    lines.append(f"- 本次实际将使用: {picked}")

    for purpose in (SCHEDULE_PURPOSE, MOOD_PURPOSE):
        cooldowns = gateway.cooldowns(purpose)
        if cooldowns:
            detail = "，".join(f"{pid} 剩余 {rem / 60:.0f} 分钟" for pid, rem in cooldowns.items())
            lines.append(f"- {purpose}冷却中: {detail}")
        else:
            lines.append(f"- {purpose}冷却中: 无")

    lines += ["", "【今日日程】"]
    lines.append(f"- 日期: {schedule_status.get('date') or '未生成'}")
    lines.append(
        f"- 来源: {schedule_status.get('source_text', '')}"
        f"，共 {schedule_status.get('slots', 0)} 个时段"
    )
    if schedule_status.get("generated_at"):
        lines.append(f"- 生成时间: {schedule_status['generated_at']}")
    if schedule_status.get("generating"):
        lines.append("- 状态: 正在后台向模型请求新日程")
    retry_after = float(schedule_status.get("retry_after") or 0.0)
    if retry_after > 0:
        lines.append(f"- 自动重试: {retry_after / 60:.0f} 分钟后（/重置日程 可立即重试）")
    last = gateway.last_result(SCHEDULE_PURPOSE)
    if last is not None:
        lines.append(f"- 上次尝试: {last.summary()}")
    if schedule_status.get("last_error"):
        lines.append(f"- 上次失败原因: {schedule_status['last_error']}")

    lines += ["", "【作息】", *_routine_lines(cfg, schedule_status, body_status)]

    lines += ["", "【当前过程】"]
    if process_status:
        name = process_status.get("name", "未知")
        duration = process_status.get("duration_minutes", 0)
        started = process_status.get("started_at", "")
        ended = process_status.get("expected_end", "")
        lines.append(f"- 正在：{name}（已持续约 {duration} 分钟）")
        if started and ended:
            lines.append(f"- 开始：{started}，预计结束：{ended}")
    else:
        lines.append("- 无活跃过程")

    lines += ["", "【情绪模型】"]
    if not cfg.mood_use_llm_for_delta:
        lines.append("- 未启用 LLM 情绪分析（仅本地规则），不消耗模型调用")
    else:
        lines.append(
            _resolve_line(
                resolver,
                "情绪模型",
                cfg.mood_provider_name or cfg.schedule_provider_name,
                available,
            )
        )
        mood_last = gateway.last_result(MOOD_PURPOSE)
        if mood_last is not None:
            lines.append(f"- 上次尝试: {mood_last.summary()}")
        lines.append(
            f"- 每 {cfg.mood_llm_interval_messages} 条消息分析一次"
            f"，失败冷却 {cfg.mood_provider_cooldown_minutes} 分钟"
            f"，群聊{'启用' if cfg.mood_enabled_in_group else '不启用'}"
        )

    lines += ["", "【身体与联动】"]
    if not cfg.soma_enabled:
        lines.append("- 生理层已关闭：只剩精力标量，不会困、不会饿、也不会攒出想说话的心思")
    elif body_status:
        snap = body_status.get("soma") or {}
        lines.append(
            f"- 轴：困意 {snap.get('sleep_pressure', 0):.0f}、睡眠债 {snap.get('sleep_debt', 0):.1f}h、"
            f"饿 {snap.get('hunger', 0):.0f}、不适 {snap.get('discomfort', 0):.0f}、"
            f"唤醒 {snap.get('arousal', 0):.0f}、想说 {snap.get('social_desire', 0):.0f}"
        )
        lines.append(
            f"- 身体推进间隔 {cfg.body_tick_seconds}s；周期第 {body_status.get('cycle_day', 1)} 天"
            f"；精力 {body_status.get('energy', 0):.0f}"
        )
        contract = body_status.get("contract")
        if isinstance(contract, dict) and contract:
            lines.append(
                f"- 联动契约 v{contract.get('v')} 已导出（{len(contract.get('feelings') or [])} 条体感、"
                f"max_chars {contract.get('form', {}).get('max_chars')}）"
            )
        else:
            lines.append("- 联动契约：尚未生成（contract_enabled 关着，或身体还没推进过）")
        signals = body_status.get("signals") or {}
        if signals.get("found"):
            lines.append(
                f"- 社交层信号：读到（上次主动开口 {signals.get('last_proactive_age', -1):.0f}s 前、"
                f"冷落计数 {signals.get('ignored_streak', 0)}）"
            )
        else:
            lines.append("- 社交层信号：没读到 humanoid_signals.json（未装自主拟人社交，或它还没写过）")
    else:
        lines.append("- 生理层已开启，但还没有角色实例")

    lines += ["", "【Token 预算】"]
    if inject_estimate:
        parts = "，".join(f"{mode} ≈ {tok}" for mode, tok in inject_estimate.items())
        lines.append(f"- 聊天时追加的上下文：{parts} token")
    lines.append(
        f"- 日程生成：每角色每天 1 次，输入约 240 + 输出按 {cfg.schedule_max_slots} 个时段计"
    )
    lines.append(
        "- 情绪分析："
        + (
            f"每 {cfg.mood_llm_interval_messages} 条私聊 1 次小 prompt（≤ 500 字）"
            if cfg.mood_use_llm_for_delta
            else "已关闭，不消耗模型调用"
        )
    )
    lines.append("- 本插件自身不会为聊天回复额外调模型：状态都靠注入，回复走 AstrBot 主链路。")

    lines += [
        "",
        "【关键参数】",
        f"- 单次生成超时 {cfg.schedule_llm_timeout_seconds}s"
        f"，每个模型尝试 {cfg.schedule_generation_max_attempts} 次"
        f"，重试间隔 {cfg.schedule_retry_interval_seconds}s",
        f"- 时段上限 {cfg.schedule_max_slots}，时间对齐 {cfg.schedule_time_granularity}",
        f"- 日程失败冷却 {cfg.schedule_provider_cooldown_minutes} 分钟"
        f"，情绪失败冷却 {cfg.mood_provider_cooldown_minutes} 分钟（两者各自记账）",
        f"- 大模型日程 {'开启' if cfg.use_llm_schedule else '关闭'}"
        f"，调试日志 {'开启' if cfg.debug_mode else '关闭'}",
    ]

    if not available:
        lines += ["", "→ 请先在 AstrBot 的「服务提供商」里配置至少一个对话模型。"]
    elif cfg.schedule_provider_name and cfg.schedule_provider_name not in available:
        lines += ["", "→ 首选模型 id 不在可用列表里：请在插件配置里用下拉框重新选择。"]

    return "\n".join(lines)