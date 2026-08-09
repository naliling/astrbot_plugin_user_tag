import json
import re
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.agent.message import TextPart

# 预设关系映射（共16种）
RELATION_PROMPTS = {
    "恋人": "你的恋人对你有着特别的偏爱与依赖，语气温柔且带点撒娇。",
    "暗恋者": "对方默默喜欢你，说话时会紧张、犹豫，但又忍不住关注你。",
    "前任": "你们曾经在一起，现在关系微妙，偶尔尴尬但又放不下。",
    "灵魂伴侣": "你们能感知彼此的情绪，默契到不需要太多言语。",
    "闺蜜": "你们是无话不谈的闺蜜，语气轻松、活泼，偶尔互损。",
    "挚友": "你们是彼此信任的挚友，语气沉稳、可靠。",
    "青梅竹马": "你们从小一起长大，熟悉到可以互相调侃，语气轻松自然。",
    "吐槽对象": "你们的关系就是互相吐槽，但从不真的伤感情，语气轻松。",
    "树洞": "对方把心事都倾诉给你，你温柔倾听，语气柔和包容。",
    "主人": "你是主人，对方是忠诚的伴侣，语气谦恭且充满信赖。",
    "学徒": "你是教导者，对方是学徒，语气认真且有点崇拜。",
    "忠诚骑士": "你发誓守护对方，语气认真、坚定，充满保护欲。",
    "搭档": "你们是并肩作战的伙伴，配合默契，语气干脆利落。",
    "猫主子": "你是被宠着的一方，对方无条件纵容你，语气带着宠溺。",
    "投喂员": "你喜欢投喂对方，对方也乐意被投喂，语气可爱又温馨。",
    "家人": "你们像家人一样，无条件支持彼此，语气温暖可靠。"
}


class UserTagPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self.data_file = str(Path(data_dir) / "user_tag.json")
        self.data = {}
        self._lock = asyncio.Lock()
        self.load_data()
        logger.info("[关系识别插件] 已加载")

    def load_data(self):
        Path(self.data_file).parent.mkdir(parents=True, exist_ok=True)
        if Path(self.data_file).exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except:
                self.data = {}
        else:
            self.data = {}

    async def save_data(self):
        async with self._lock:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get_relation_prompt(self, relation: str) -> str:
        if relation in RELATION_PROMPTS:
            return RELATION_PROMPTS[relation]
        return f"你们的关系是「{relation}」，请根据这个关系自然而然地调整语气与态度。"

    @filter.command("设置关系")
    async def set_relation(self, event: AstrMessageEvent):
        match = re.search(r'设置关系\s+(.+)', event.message_str)
        if not match:
            yield event.plain_result(
                "用法：设置关系 恋人\n"
                "可选预设：恋人 / 暗恋者 / 前任 / 灵魂伴侣 / 闺蜜 / 挚友 / "
                "青梅竹马 / 吐槽对象 / 树洞 / 主人 / 学徒 / 忠诚骑士 / 搭档 / "
                "猫主子 / 投喂员 / 家人\n"
                "也可以自定义任意关系名称。"
            )
            return
        relation = match.group(1).strip()
        if not relation:
            yield event.plain_result("关系名称不能为空。")
            return
        qq = str(event.get_sender_id())
        self.data[qq] = relation
        await self.save_data()
        yield event.plain_result(f"已成功将你的关系设置为：【{relation}】")

    @filter.command("清除关系")
    async def clear_relation(self, event: AstrMessageEvent):
        qq = str(event.get_sender_id())
        if qq in self.data:
            del self.data[qq]
            await self.save_data()
            yield event.plain_result("已清除你的关系记录。")
        else:
            yield event.plain_result("你当前没有设置任何关系。")

    @filter.command("查看我的关系")
    async def my_relation(self, event: AstrMessageEvent):
        qq = str(event.get_sender_id())
        default_rel = self.config.get("default_relation", "伙伴")
        relation = self.data.get(qq, default_rel)
        yield event.plain_result(f"你当前的关系是：{relation}")

    @filter.command("查看所有关系")
    async def list_relations(self, event: AstrMessageEvent):
        admin_qq = self.config.get("admin_qq", [])
        if str(event.get_sender_id()) not in [str(a).strip() for a in admin_qq]:
            yield event.plain_result("权限不足，仅管理员可查看。")
            return
        if not self.data:
            yield event.plain_result("当前没有任何关系记录。")
            return
        total = len(self.data)
        lines = [f"当前共有 {total} 段关系记录", "——————————————"]
        for uid, rel in self.data.items():
            lines.append(f"{uid} → {rel}")
        lines.append("——————————————")
        yield event.plain_result("\n".join(lines))

    @filter.command("关系列表")
    async def list_preset(self, event: AstrMessageEvent):
        lines = ["可用的预设关系："]
        for rel in sorted(RELATION_PROMPTS.keys()):
            lines.append(f"  • {rel}")
        lines.append("\n也可以自定义任意关系名称。")
        yield event.plain_result("\n".join(lines))

    @filter.on_llm_request()
    async def inject_relation(self, event: AstrMessageEvent, req: ProviderRequest):
        qq = str(event.get_sender_id())
        default_rel = self.config.get("default_relation", "伙伴")
        relation = self.data.get(qq, default_rel)
        prompt_text = (
            f"<relation_hint>用户与你的关系是「{relation}」。"
            f"{self.get_relation_prompt(relation)}</relation_hint>"
        )
        req.extra_user_content_parts.append(
            TextPart(text=prompt_text).mark_as_temp()
        )
        logger.info(f"已为用户 {qq} 注入关系: {relation}")
