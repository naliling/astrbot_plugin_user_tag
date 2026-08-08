import json
import re
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.agent.message import TextPart

# 已移除 @register 装饰器，AstrBot 会自动识别继承自 Star 的类
class Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 使用标准数据目录，插件名固定为 astrbot_plugin_user_tag
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_user_tag"
        self.data_file = str(Path(data_dir) / "user_tag.json")
        self.data = {}
        self._lock = asyncio.Lock()
        self.load_data()
        logger.info("[关系识别插件] 已加载")

    def load_data(self):
        Path(self.data_file).parent.mkdir(parents=True, exist_ok=True)
        if Path(self.data_file).exists():
            with open(self.data_file, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {}

    async def save_data(self):
        async with self._lock:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    @filter.command("设置关系")
    async def set_relation(self, event: AstrMessageEvent):
        match = re.search(r'设置关系\s+(\S+)', event.message_str)
        if not match:
            yield event.plain_result("⚠️ 请指定关系名称，例如：设置关系 恋人")
            return
        relation = match.group(1).strip()
        if not relation:
            yield event.plain_result("⚠️ 关系名称不能为空")
            return
        qq = str(event.get_sender_id())
        self.data[qq] = relation
        await self.save_data()
        yield event.plain_result(f"✅ 已成功将你的关系设置为：【{relation}】")

    @filter.command("清除关系")
    async def clear_relation(self, event: AstrMessageEvent):
        qq = str(event.get_sender_id())
        if qq in self.data:
            del self.data[qq]
            await self.save_data()
            yield event.plain_result("已清除你的关系记录。")
        else:
            yield event.plain_result("你当前没有设置任何关系。")

    @filter.command("查看所有关系")
    async def list_relations(self, event: AstrMessageEvent):
        admin_qq = self.config.get("admin_qq", [])
        if str(event.get_sender_id()) not in [str(a).strip() for a in admin_qq]:
            yield event.plain_result("权限不足，仅管理员可查看。")
            return
        if not self.data:
            yield event.plain_result("当前没有任何关系记录。")
        else:
            lines = ["——————————————"]
            for uid, rel in self.data.items():
                lines.append(f"{uid},{rel}")
            lines.append("——————————————")
            yield event.plain_result("\n".join(lines))

    @filter.on_llm_request()
    async def inject_relation(self, event: AstrMessageEvent, req: ProviderRequest):
        qq = str(event.get_sender_id())
        logger.info(qq)
        if qq in self.data:
            relation = self.data[qq]
            if relation:
                req.extra_user_content_parts.append(TextPart(text=(f"<relation_hint>{relation}</relation_hint>")).mark_as_temp())
                logger.info(f"✅ 已为用户 {qq} 注入关系: {relation}")
