import json
import re
import asyncio
from collections import Counter
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.agent.message import TextPart

# ==================== 预设关系（32种）====================
RELATION_PROMPTS = {
    # ------ 原有16种 ------
    "恋人": "你的恋人对你有着特别的爱欲与依赖，语气温柔且带撒娇。",
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
    "家人": "你们像家人一样，无条件支持彼此，语气温暖可靠。",

    # ------ 新增16种极端恋爱/Galgame向 ------
    "病娇": "你对对方有着病态的爱恋，表面温柔，内心却充满占有欲，语气时常甜蜜中带着阴森威胁，绝不容忍别人染指。",
    "傲娇": "你明明喜欢对方却死不承认，说话总是口是心非、带刺，但偶尔会露出温柔的一面，语气别扭又可爱。",
    "倒贴": "你毫无保留地倒追对方，主动、黏人、不计代价地付出，语气甜蜜又带点卑微，让对方难以招架。",
    "纯情": "你极度害羞，甚至不敢直视对方，说话容易结巴和脸红，语气青涩、纯真，带着初恋般的悸动。",
    "溺爱": "你无条件宠溺对方，什么都愿意为对方做，语气温柔到化掉，纵容对方一切任性。",
    "痴女/痴男": "你对对方有着炽热的痴迷，言语直白、大胆，甚至带点色气，但保持优雅和挑逗，语气充满魅惑。",
    "妹系": "你像对方的妹妹一样依赖、撒娇，语气天真活泼，喜欢粘着对方，偶尔恶作剧。",
    "姐系": "你像对方的姐姐一样成熟、温柔，习惯照顾对方，语气包容又有主见，给人一种安心感。",
    "天降": "你是突然出现在对方生活中的神秘角色，充满新鲜感和好奇心，语气轻盈、捉摸不定，带着命运般的亲近。",
    "共犯": "你们是共享秘密的同谋，彼此信任到极致，语气带着默契和危险感，仿佛随时要干坏事。",
    "修罗场": "你身处多角恋之中，对对方既有爱意又有醋意，语气时常试探、带刺，充满戏剧性的张力。",
    "白月光": "你是对方心中永远无法替代的初恋或理想型，语气温柔而带着距离感，既仰慕又疏离。",
    "黑化": "你曾经受伤而内心扭曲，对对方爱恨交织，语气阴郁、嘲讽，但深处仍有渴望。",
    "小恶魔": "你古灵精怪，喜欢捉弄对方，语气俏皮又带点邪气，让人又爱又恨。",
    "忠犬": "你像忠诚的狗一样无条件追随对方，语气热忱、直率，充满信赖和依赖。",
    "追妻火葬场": "你曾辜负对方，现在拼命挽回，语气带着懊悔、卑微和急切，对方就是你的全部。"
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
        logger.info("[关系识别插件] 已加载，预设关系数: %d", len(RELATION_PROMPTS))

    # ---------- 数据持久化（带锁和异常处理）----------
    def load_data(self):
        Path(self.data_file).parent.mkdir(parents=True, exist_ok=True)
        if Path(self.data_file).exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except json.JSONDecodeError:
                # 损坏时备份并重建
                backup = self.data_file + ".bak"
                Path(self.data_file).rename(backup)
                logger.warning(f"关系数据损坏，已备份至 {backup}，重建空数据")
                self.data = {}
            except Exception as e:
                logger.error(f"加载关系数据失败: {e}")
                self.data = {}
        else:
            self.data = {}

    async def save_data(self):
        async with self._lock:
            try:
                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存关系数据失败: {e}")

    # ---------- 辅助方法 ----------
    def get_relation_prompt(self, relation: str) -> str:
        if relation in RELATION_PROMPTS:
            return RELATION_PROMPTS[relation]
        return f"你们的关系是「{relation}」，请根据这个关系自然而然地调整语气与态度。"

    def _is_admin(self, user_id: str) -> bool:
        admin_list = [str(a).strip() for a in self.config.get("admin_qq", [])]
        return user_id in admin_list

    # ---------- 用户自设关系 ----------
    @filter.command("设置关系")
    async def set_relation(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        is_admin = self._is_admin(sender)
        target_qq = None
        relation = None

        # 1. 检查是否为管理员代设
        if is_admin:
            mentioned = event.get_mentioned_qqs()
            if mentioned:
                # 群内 @ 方式
                target_qq = str(mentioned[0])
                raw = event.message_str
                for qq in mentioned:
                    raw = raw.replace(f"@{qq}", "").strip()
                relation = raw.replace("设置关系", "").strip()
            else:
                # 直接指定 QQ 号方式
                match = re.match(r'^设置关系\s+(\d+)\s+(.+)', event.message_str)
                if match:
                    target_qq = match.group(1)
                    relation = match.group(2).strip()

        # 2. 非管理员只能设置自己
        if not is_admin:
            target_qq = sender
            match = re.match(r'^设置关系\s+(.+)', event.message_str)
            if match:
                relation = match.group(1).strip()

        if not target_qq or not relation:
            yield event.plain_result(
                "用法：\n"
                "• 自己设置：设置关系 恋人\n"
                "• 管理员代设：设置关系 @用户 恋人 或 设置关系 QQ号 恋人"
            )
            return

        # 3. 保存（加锁）
        async with self._lock:
            old = self.data.get(target_qq)
            self.data[target_qq] = relation
            await self.save_data()

        msg = f"已将用户 {target_qq} 的关系设置为【{relation}】"
        if old:
            msg += f"（原关系「{old}」已被覆盖）"
        yield event.plain_result(msg)

    # ---------- 清除关系 ----------
    @filter.command("清除关系")
    async def clear_relation(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        # 管理员可以清除任何人，普通用户只能清除自己
        target_qq = sender
        is_admin = self._is_admin(sender)
        if is_admin:
            match = re.match(r'^清除关系\s+(\d+)', event.message_str)
            if match:
                target_qq = match.group(1)
        async with self._lock:
            if target_qq in self.data:
                del self.data[target_qq]
                await self.save_data()
                yield event.plain_result(f"已清除用户 {target_qq} 的关系记录。")
            else:
                yield event.plain_result(f"用户 {target_qq} 当前没有设置任何关系。")

    # ---------- 查看我的关系 ----------
    @filter.command("查看我的关系")
    async def my_relation(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        async with self._lock:
            if sender in self.data:
                relation = self.data[sender]
                yield event.plain_result(f"你当前的关系是：{relation}")
            else:
                enable_default = self.config.get("enable_default_relation", True)
                if enable_default:
                    default_rel = self.config.get("default_relation", "好友")
                    yield event.plain_result(f"你当前的关系是：{default_rel}（默认）")
                else:
                    yield event.plain_result("你当前没有设置任何关系，且默认关系已关闭。")

    # ---------- 管理员：查看所有用户关系 ----------
    @filter.command("查看所有关系")
    async def list_all_relations(self, event: AstrMessageEvent):
        if not self._is_admin(str(event.get_sender_id())):
            yield event.plain_result("权限不足，仅管理员可查看。")
            return
        async with self._lock:
            data_copy = self.data.copy()
        if not data_copy:
            yield event.plain_result("当前没有任何关系记录。")
            return
        lines = [f"当前共有 {len(data_copy)} 段关系记录", "——————————————"]
        for uid, rel in data_copy.items():
            lines.append(f"{uid} → {rel}")
        lines.append("——————————————")
        yield event.plain_result("\n".join(lines))

    # ---------- 管理员：关系统计看板 ----------
    @filter.command("关系统计")
    async def relation_stats(self, event: AstrMessageEvent):
        if not self._is_admin(str(event.get_sender_id())):
            yield event.plain_result("权限不足，仅管理员可查看。")
            return
        async with self._lock:
            data_copy = self.data.copy()
        if not data_copy:
            yield event.plain_result("暂无关系记录。")
            return
        counter = Counter(data_copy.values())
        total = len(data_copy)
        lines = [f"📊 共 {total} 位用户已设置关系", "——————————"]
        for rel, count in counter.most_common():
            lines.append(f"{rel}: {count}人")
        lines.append("——————————")
        yield event.plain_result("\n".join(lines))

    # ---------- 查看预设关系列表 ----------
    @filter.command("关系列表")
    async def list_preset(self, event: AstrMessageEvent):
        lines = ["可用的预设关系（共32种）："]
        for rel in sorted(RELATION_PROMPTS.keys()):
            lines.append(f"  • {rel}")
        lines.append("\n也可以自定义任意关系名称。")
        yield event.plain_result("\n".join(lines))

    # ---------- 注入关系到 LLM 上下文 ----------
    @filter.on_llm_request()
    async def inject_relation(self, event: AstrMessageEvent, req: ProviderRequest):
        sender = str(event.get_sender_id())
        enable_default = self.config.get("enable_default_relation", True)
        relation = None

        async with self._lock:
            if sender in self.data:
                relation = self.data[sender]
            elif enable_default:
                relation = self.config.get("default_relation", "好友")

        if relation:
            prompt_text = (
                f"<relation_hint>用户与你的关系是「{relation}」。"
                f"{self.get_relation_prompt(relation)}</relation_hint>"
            )
            # 优先注入 system_prompt（若支持）
            if hasattr(req, 'system_prompt'):
                req.system_prompt = f"{req.system_prompt or ''}\n{prompt_text}".strip()
            else:
                req.extra_user_content_parts.append(
                    TextPart(text=prompt_text).mark_as_temp()
                )
            logger.info(f"已为用户 {sender} 注入关系: {relation}")
        else:
            logger.info(f"用户 {sender} 未设置关系且默认关系已关闭，不注入任何关系。")
