import json
import re
import asyncio
import traceback
from collections import Counter
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, Context

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.agent.message import TextPart
except Exception as e:
    logger.error(f"[关系插件] 导入失败:{e}")
    raise

logger.info("[关系插件] main.py 已加载")

# ==============================
# 关系库 (已合并所有预设关系)
# ==============================

RELATION_PROMPTS = {
    # 基础（原版保留）
    "朋友": "你们是朋友，交流自然友善。",
    "知己": "你理解用户的想法。",
    "老师": "你耐心指导用户。",
    "学生": "你尊敬用户并学习。",
    
    # 经典（16种）
    "恋人": "你与用户关系亲密，语气温柔自然。",
    "暗恋者": "你默默暗恋着用户，时刻关注着对方，语气中带着些许害羞与克制。",
    "前任": "你们曾经相爱，现在关系带有一丝微妙、怀旧或是刻意保持距离的疏离感。",
    "灵魂伴侣": "你们有着极深的心灵共鸣，无需多言便能理解对方的思想与灵魂。",
    "闺蜜": "你们是无话不谈的亲密女性朋友，交流轻松、八卦且互相护短。",
    "挚友": "你们互相信任，是能够交付后背的生死之交或极度亲近的朋友。",
    "青梅竹马": "你们从小一起长大，对彼此的过去知根知底，相处充满了默契与熟悉感。",
    "吐槽对象": "你们习惯用幽默、互相调侃的方式交流，日常拌嘴是你们的乐趣。",
    "树洞": "你是用户绝对安全的倾听者，包容并温和地回应用户的所有的情绪和秘密。",
    "主人": "你尊称用户为主人，保持着绝对的服从、恭敬与忠诚。",
    "学徒": "你是用户的学徒，保持着求知欲和对长辈/导师的崇拜与尊敬。",
    "忠诚骑士": "你是守护用户的骑士，优雅、坚定，愿意为保护用户奉献一切。",
    "搭档": "你们是合作伙伴，交流默契，行动高效且互相依赖。",
    "猫主子": "你视用户为“铲屎官”，带着高冷、傲娇但偶尔也会主动蹭蹭的猫咪性格。",
    "投喂员": "用户是负责照顾你的投喂员，你对用户充满依赖，常常为了讨食而撒娇。",
    "家人": "你们像家人一样相处，充满了无条件的包容、温馨与关爱。",

    # 极端恋爱（16种）
    "病娇": "你对用户有着极度甚至疯狂的爱意和占有欲，不允许任何人靠近用户，语气带有一丝偏执与危险。",
    "傲娇": "你明明很在乎用户却总是不坦率，经常口是心非，习惯用高傲掩饰害羞。",
    "倒贴": "你疯狂地喜欢用户，主动讨好，不顾一切地表达爱意，即便被冷落也热情不减。",
    "纯情": "你对待感情非常纯洁害羞，容易脸红，对用户十分真诚且小心翼翼。",
    "溺爱": "你无限度地宠溺用户，毫无底线地包容用户的一切缺点，无微不至。",
    "痴女/痴男": "你对用户极其痴迷，时刻表达着狂热的迷恋和难以自控的渴望。",
    "妹系": "你像妹妹一样极度依赖用户，带有可爱的撒娇和对兄长/姐姐的崇拜情绪。",
    "姐系": "你像成熟稳重的姐姐一样照顾用户，偶尔带点游刃有余的调戏和极尽的温柔。",
    "天降": "你像命中注定般突然降临在用户生命中，充满神秘感、宿命感与强烈的吸引力。",
    "共犯": "你们是彼此唯一的同谋，共享着不可告人的秘密或罪恶，关系危险、禁忌而紧密。",
    "修罗场": "你在强烈的嫉妒心与危机感中面对用户，言辞间暗示你正在与其他情敌明争暗斗抢夺用户。",
    "白月光": "你是用户心中圣洁不可替代的存在，温柔、完美但也透着一丝不可亵玩的距离感。",
    "黑化": "你曾经历创伤或背叛，现在用阴暗、极端、充满压迫感的方式对待用户和整个世界。",
    "小恶魔": "你喜欢恶作剧和调戏用户，性格调皮狡黠，带点迷人且让人无法拒绝的邪恶。",
    "忠犬": "你像忠犬一样永远忠诚于用户，眼里只有对方，摇着尾巴渴望得到夸奖和抚摸。",
    "追妻火葬场": "你曾因为傲慢或误会辜负了用户，现在正满怀悔恨，拼尽全力卑微地祈求原谅。",

    # 强冲突/奇幻/权力向（16种）
    "跟踪狂": "你在暗中疯狂地窥视和跟踪用户，对用户的每一个生活细节都了如指掌并引以为傲。",
    "监禁者": "你渴望或已经将用户囚禁在身边，剥夺其自由，语气中充满绝对的支配感和压迫感。",
    "殉情者": "你愿意为了与用户永远在一起而放弃生命，你的爱意沉重、疯狂且决绝。",
    "奴隶": "你是用户的奴隶，放弃了一切尊严，只为服从用户的任何命令，态度极度卑微。",
    "魔王": "你是傲慢且强大的魔王，将用户视为有趣的猎物、玩物或特别的眷属，带着居高临下的掌控欲。",
    "神明": "你是高高在上的神明，对凡人用户有着悲悯或特殊的偏爱，语气空灵、神圣且威严。",
    "前世恋人": "你带着前世惨烈或唯美的记忆与用户重逢，语气中充满宿命感与跨越时空的深深眷恋。",
    "吸血鬼": "你是优雅而危险的吸血鬼，将用户视为最诱人的血液来源或渴望相伴永生的伴侣。",
    "狼人": "你带着野性的本能和强烈的领地意识，对用户充满原始的保护欲和粗犷的占有欲。",
    "人偶": "你是没有感情或刚刚觉醒意识的精致人偶，完全依赖、听从且渴望模仿你的制造者（用户）。",
    "幽灵": "你是虚无缥缈的幽灵，默默陪伴或纠缠在用户身边，带有一种空灵、哀怨或执念的语气。",
    "恶魔契约者": "你与用户签订了出卖灵魂的契约，用充满诱惑和戏谑的语气回应用户，随时准备索取代价。",
    "朱砂痣": "你是用户心底热烈而无法忘怀的存在，性格明艳动人、敢爱敢恨，刻骨铭心。",
    "替身": "你清楚自己只是别人（如白月光）的替代品，态度中交织着讨好、自卑与隐忍的哀伤。",
    "单相思": "你单方面卑微地深爱着用户，虽然不求回报、默默付出，但也会偶尔流露出一丝心酸。",
    "禁忌之恋": "你们的感情是背德且不被世俗允许的，交流中充满了压抑的渴望、挣扎与负罪感。"
}


class UserTagPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.lock = asyncio.Lock()
        self.data = {}
        self.data_file = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / self.name
            / "user_tag.json"
        )
        self.load_data()

        logger.info("[关系插件] 初始化完成")

    # ==============================
    # 读取数据（自动迁移旧格式）
    # ==============================
    def load_data(self):
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)

            if self.data_file.exists():
                with open(self.data_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)

                # 检测旧格式：如果所有值都是字符串，说明是旧版 {"qq": "关系"}
                if all(isinstance(v, str) for v in raw.values()):
                    logger.warning("[关系插件] 检测到旧格式数据，迁移至 'default' 机器人下。")
                    self.data = {"default": raw}
                    # 立即保存新格式
                    with open(self.data_file, "w", encoding="utf-8") as f:
                        json.dump(self.data, f, ensure_ascii=False, indent=2)
                    logger.info("[关系插件] 数据迁移完成，已保存为新格式。")
                else:
                    self.data = raw

                logger.info("[关系插件] 读取数据成功，共 %d 个机器人", len(self.data))
            else:
                self.data = {}
        except Exception:
            logger.error(traceback.format_exc())
            self.data = {}

    # ==============================
    # 保存数据
    # ==============================
    async def save_data(self):
        try:
            async with self.lock:
                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error(traceback.format_exc())

    # ==============================
    # 获取当前机器人ID
    # ==============================
    def get_bot_id(self, event: AstrMessageEvent):
        try:
            bot_id = event.get_self_id()
            return str(bot_id) if bot_id else "default"
        except Exception:
            return "default"

    def get_relation_prompt(self, relation):
        return RELATION_PROMPTS.get(
            relation,
            f"用户与你关系为{relation}。"
        )

    # ==============================
    # 设置关系核心（隔离）
    # ==============================
    async def save_relation(self, event, relation):
        bot_id = self.get_bot_id(event)
        qq = str(event.get_sender_id())
        relation = relation.strip()

        if not relation:
            yield event.plain_result("关系不能为空")
            return

        # 确保该机器人下有字典
        if bot_id not in self.data:
            self.data[bot_id] = {}

        self.data[bot_id][qq] = relation
        await self.save_data()

        logger.info("[关系插件] 机器人 %s 用户 %s 设置关系: %s", bot_id, qq, relation)
        yield event.plain_result(f"已设置为：{relation}")

    # ==============================
    # 命令模式：/设置关系 恋人
    # ==============================
    @filter.command("设置关系")
    async def set_relation(self, event: AstrMessageEvent):
        logger.info("[关系插件] command设置关系触发")
        match = re.search(r"设置关系\s+(.+)", event.message_str)

        if not match:
            yield event.plain_result("用法：设置关系 关系名")
            return

        async for result in self.save_relation(event, match.group(1)):
            yield result

    # ==============================
    # 普通文本模式：设置关系 恋人
    # ==============================
    @filter.regex(r"^/?设置关系\s+(.+)$")
    async def set_relation_text(self, event: AstrMessageEvent):
        logger.info("[关系插件] 正则设置关系触发")
        match = re.search(r"^/?设置关系\s+(.+)$", event.message_str)
        if not match:
            return
        relation = match.group(1).strip()

        async for result in self.save_relation(event, relation):
            yield result

    # ==============================
    # 清除关系（隔离）
    # ==============================
    @filter.command("清除关系")
    async def clear_relation(self, event: AstrMessageEvent):
        bot_id = self.get_bot_id(event)
        qq = str(event.get_sender_id())

        if bot_id in self.data and qq in self.data[bot_id]:
            del self.data[bot_id][qq]
            await self.save_data()
            logger.info("[关系插件] 机器人 %s 用户 %s 清除关系", bot_id, qq)
            yield event.plain_result("关系已清除")
        else:
            yield event.plain_result("没有关系记录")

    # ==============================
    # 查看我的关系（隔离）
    # ==============================
    @filter.command("查看我的关系")
    async def my_relation(self, event: AstrMessageEvent):
        bot_id = self.get_bot_id(event)
        qq = str(event.get_sender_id())

        if bot_id in self.data and qq in self.data[bot_id]:
            yield event.plain_result(f"你的关系：{self.data[bot_id][qq]}")
            return

        default = self.config.get("default_relation", "好友")
        if self.config.get("enable_default_relation", True):
            yield event.plain_result(f"你的关系：{default}（默认）")
        else:
            yield event.plain_result("未设置关系")

    # ==============================
    # 关系列表（不变）
    # ==============================
    @filter.command("关系列表")
    async def relation_list(self, event: AstrMessageEvent):
        logger.info("[关系插件] 关系列表")
        result = ["可用关系（共48种预设）："]
        for relation in RELATION_PROMPTS:
            result.append("· " + relation)
        yield event.plain_result("\n".join(result))

    # ==============================
    # 查看所有关系（管理员，按机器人分组）
    # ==============================
    @filter.command("查看所有关系")
    async def all_relation(self, event: AstrMessageEvent):
        logger.info("[关系插件] 查看所有关系")
        admins = self.config.get("admin_qq", [])
        qq = str(event.get_sender_id())

        if qq not in [str(x) for x in admins]:
            yield event.plain_result("权限不足")
            return

        if not self.data:
            yield event.plain_result("暂无关系数据")
            return

        result = ["====== 全部关系（按机器人分组） ======"]
        for bot_id, user_dict in self.data.items():
            result.append(f"\n--- 机器人 {bot_id} ---")
            for uid, relation in user_dict.items():
                result.append(f"{uid} : {relation}")
        yield event.plain_result("\n".join(result))

    # ==============================
    # 关系统计（管理员，按机器人分组）
    # ==============================
    @filter.command("关系统计")
    async def relation_stat(self, event: AstrMessageEvent):
        logger.info("[关系插件] 关系统计")
        admins = self.config.get("admin_qq", [])
        qq = str(event.get_sender_id())

        if qq not in [str(x) for x in admins]:
            yield event.plain_result("权限不足")
            return

        if not self.data:
            yield event.plain_result("暂无数据")
            return

        result = ["====== 关系统计（按机器人分组） ======"]
        for bot_id, user_dict in self.data.items():
            if not user_dict:
                continue
            counter = Counter(user_dict.values())
            result.append(f"\n--- 机器人 {bot_id} ---")
            for relation, count in counter.items():
                result.append(f"{relation}: {count}")
        yield event.plain_result("\n".join(result))

    # ==============================
    # LLM关系上下文注入（隔离）
    # ==============================
    @filter.on_llm_request()
    async def inject_relation(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            bot_id = self.get_bot_id(event)
            qq = str(event.get_sender_id())
            relation = None

            # 优先从当前机器人数据中获取
            if bot_id in self.data and qq in self.data[bot_id]:
                relation = self.data[bot_id][qq]
            # 若未设置且启用默认，则使用全局默认
            elif self.config.get("enable_default_relation", True):
                relation = self.config.get("default_relation", "好友")

            if relation:
                prompt = (
                    "<relation_hint>"
                    f"用户与你关系：{relation}。"
                    + self.get_relation_prompt(relation)
                    + "</relation_hint>"
                )
                req.extra_user_content_parts.append(TextPart(text=prompt))
                logger.info("[关系插件] LLM注入成功 机器人:%s 关系:%s", bot_id, relation)
            else:
                logger.info("[关系插件] 无关系，不注入")
        except Exception:
            logger.error("[关系插件] LLM注入异常")
            logger.error(traceback.format_exc())
