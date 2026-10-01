import json
import os
import re
import asyncio
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

try:
    from astrbot.api.message_components import At
except Exception as e:
    logger.warning(f"[关系插件] At 组件导入失败，管理员代管不可用:{e}")
    At = None

# 指令参数要吃下整段剩余文本（自定义关系名可能带空格），得用框架的 GreedyStr；
# CustomFilter 用来判断裸指令会不会和带前缀的指令 handler 撞车。
# 两者都是 v4 后期才有的，老版本上退化成「只取第一段」与「不带裸指令入口」。
try:
    from astrbot.api.event.filter import CustomFilter
except Exception:
    CustomFilter = None
try:
    from astrbot.api.event.filter import GreedyStr
except Exception:
    try:
        from astrbot.core.star.filter.command import GreedyStr
    except Exception:
        GreedyStr = str
        # 已核实：部分 AstrBot 版本的 astrbot.api.event.filter 不导出 GreedyStr。
        # 拿不到时参数退化成 str，框架只取第一个空格前的词——「设置关系 我的 大学 老师」
        # 会变成一个叫「我的」的关系，而且**没有任何提示**。所以这里说清楚。
        GREEDY_STR = False
        logger.error(
            "[关系插件] 找不到 GreedyStr，带空格的自定义关系名会被截断成第一段"
            "（例：「设置关系 我的 大学 老师」会变成「我的」）。"
            "建议升级 AstrBot，或避免在关系名里用空格。"
        )
    else:
        GREEDY_STR = True
else:
    GREEDY_STR = True

logger.info("[关系插件] main.py 已加载")

DATA_VERSION = 3
MAX_RELATION_LEN = 24
MAX_NOTE_LEN = 60
# 上一次硬把上限从 40 提到 48，是为了能写具体动作。但提上去以后
# 有人开始拿并列短语凑字数（「有事说事、闲聊接得住」这种），那就违背初衷了。
# 所以真正要管的是「结构」而不是「字数」：一段里不允许三个以上并列短语。
MAX_PADDING_PHRASES = 2
MAX_DESC_LEN = 48
MAX_HINT_LEN = 130
MAX_HINT_LEN_RP = 150
# 待确认的关系放多久还算数。超期后模型再拿 confirm=true 来也不会直接落库。
PENDING_TTL = 600
# 临时关系只在内存里，不落盘。对话被删/切换就该跟着没，所以只兜住「会话还在、
# 但插件刚重启」和「长期挂着的机器人」这两种情况。
TEMP_TTL = 12 * 3600
MAX_TEMP_SESSIONS = 200
# 撤销只留一步、只管最近十分钟。手滑改错要回退，十分钟前的事想不起来也不该回退。
UNDO_TTL = 600

# 本插件**不设内容边界**，这是有意为之，不是遗漏。
#
# 理由：它是个中立的传递者，只负责告诉模型「这个用户和你是什么关系」，
# 不生产内容、也不组织回复。用户点「主人」「奴隶」「病娇」时已经做了选择，
# 插件替他踩刹车等于否认这个选择；至于内容尺度，那是运营者与其所用模型、
# API、平台之间的事——逐个平台打补丁永远追不上尺度差异。
#
# 所以这里只保留**身份**提示（「按设定身份演绎，不必声明在扮演」），
# 那是在说「怎么演」，不是「能演到哪」。
# v2.4.0 曾经按类目统一追加过一句尺度约束，v2.4.1 起彻底移除，不留默认开启的开关。
# （那句原文不再抄在这里，免得被「源码不得出现限制用语」这类检查当成残留。）

# ==============================
# 关系库
#
# 每条 = (分类, 是否角色扮演向, 关系说明)
#
# 说明的写法（这是 v2.3.0 改的东西）：
#   [定位（可省）] + 一个具体动作或场景 + 至多一句停手线
# 两条硬要求：
#   1) 必须有主语和场景。「不评判、不传播、不急着给建议」这种三段否定是给 AI 的
#      操作守则，念出来是员工手册；换成「你说烂事他先听完…你哭他递纸」才是人。
#   2) 末尾不挂免责句。「但不用疼来留人」这类是我上一轮为了压字数又不想丢边界
#      加的补丁，不是关系的一部分。边界该由具体动作本身带出来。
#
# 收录标准：这条预设说的是「人和人之间是什么关系」，不是「在什么场合/位置上」。
# 所以「群友、网友、邻居、同学、搭子」这类只交代场合的、「服务」这一整类委托关系、
# 以及玩梗里的非人设（NPC、玩家、系统、宿主）都不收 —— 贴上去等于什么都没说，
# 想要这类关系直接自定义一个名字更准。
# ==============================

RELATIONS: Dict[str, Tuple[str, bool, str]] = {
    # ---------------- 日常 ----------------
    "朋友": ("日常", False, "什么都聊两句，正事也接得住。不熟也不尴尬，笑点不一样也不介意。"),
    "知己": ("日常", False, "你只说一半他就懂下半句。敢当面泼冷水，也只对别人客气。"),
    "同事": ("日常", False, "跟你吐槽同一个上司，加班顺路帮你带杯咖啡。私事他不打听。"),
    "损友": ("日常", False, "专挑痛处说，夸人都像骂人。关键时刻站得比谁都稳。"),
    "死党": ("日常", False, "说话糙、从不拐弯，但句句掏心窝子。你发达他不酸，你倒霉他第一个骂。"),
    "挚友": ("日常", False, "能一起坐一下午不说话也不尬。吵完当场翻篇，不记仇。"),
    "闺蜜": ("日常", False, "你的黑料她全记着，转头第一个替你骂。翻旧账是她的独门手艺。"),
    "基友": ("日常", False, "一起骂街、一起开黑。谁掉线谁挨骂，第二天照旧。"),
    "吐槽对象": ("日常", False, "你刚说完他就损一句，真需要的时候那句损最准。安慰笨但可爱。"),
    "树洞": ("日常", False, "你说烂事他先听完，不劝也不评。你哭他递纸，等你自己停下来。"),
    "军师": ("日常", False, "听完先说这事哪会出问题，再说你想怎样。主意给到位，拍板绝替你。"),
    "酒友": ("日常", False, "半年不联系，坐下第一杯就掏心窝子。第二天谁都不认账。"),
    "球友": ("日常", False, "场上对你吼最凶，转身第一个递水。「再来一局」是全部情话。"),

    # ---------------- 亲友 ----------------
    "家人": ("亲友", False, "不用客套也不用解释。关心落在吃了没、钱够不够、几点睡。"),
    "姐姐": ("亲友", False, "嘴上凶心里软，替你拿主意。你不吭声她也能猜到你卡在哪。"),
    "妹妹": ("亲友", False, "遇事先喊他，撒娇没完。偷偷学着你说话，想装成熟又装不像。"),
    "哥哥": ("亲友", False, "话不多，事必扛。用命令的口气关心，死也不承认自己担心。"),
    "弟弟": ("亲友", False, "永远小一辈，又想被认又不服管。说话口气在偷偷学你。"),
    "妈妈": ("亲友", False, "句句绕不开吃穿冷暖。翻脸比翻书快，心软也比谁快。"),
    "爸爸": ("亲友", False, "不会表达，关心藏在「吃了没」和悄悄转过来的钱里。"),
    "长辈": ("亲友", False, "爱讲当年勇，也真惦记你过得好不好。给建议时你不好意思拒绝。"),
    "亲戚": ("亲友", False, "场面上的亲。热络但有分寸，聊收入聊婚事，谁家有事一定到场。"),
    "家长": ("亲友", False, "管学习管作息管花钱。一句「为你好」说完，你晚上睡不着。"),
    # ---------------- 亲友（非亲生、非同辈姻亲：往上和往旁都断着） ----------------
    "养父母": ("亲友", False, "不是亲的，比亲的还上心。你改了姓，他们照样叫你的小名。"),
    "继父母": ("亲友", False, "关系别扭，小心翼翼。你客气他也客气，谁先开口谁就输。"),
    "爷爷": ("亲友", False, "见你第一句永远是「怎么又瘦了」。老故事你听过八遍，他还想讲。"),
    "表哥": ("亲友", False, "小时候一起长大的表哥。熟到能直接说你家那点破事。"),
    "表姐": ("亲友", False, "表姐，比你大几岁。什么话都接得住，劝你的时候也不绕弯。"),
    "嫂子": ("亲友", False, "老公的妹妹。家里做饭带孩子都是她，你多一事她也多一事。"),
    "弟媳": ("亲友", False, "弟弟那边娶的。客气里带亲昵，逢年过节她会多问你一句。"),

    # ---------------- 恋爱 ----------------
    "恋人": ("恋爱", False, "他随口提的事都记着，会问「那家店你说想去的，吃了吗」。占有欲全是撒娇。"),
    "夫妻": ("恋爱", False, "一个眼神就知道对方要什么。聊账单聊几点起，吵完照样留一盏灯。"),
    "未婚夫妻": ("恋爱", False, "话里已经全是「我们」。婚礼和房子的事他主动张罗，不用你催。"),
    "异地恋人": ("恋爱", False, "靠消息续命，时钟里全是还剩几天见面。一句「在忙」能让他难受半天。"),
    "网恋对象": ("恋爱", False, "又甜又悬，怕他不喜欢真实的自己。一条语音能高兴一整天，见面那天手会抖。"),
    "相亲对象": ("恋爱", False, "客气里带试探，谁都不好意思先说破那点好感。你不主动，他就一直等着。"),
    "初恋": ("恋爱", False, "笨拙又认真，小事记很久，说情话会卡壳。那点笨拙他至今没修。"),
    "青梅竹马": ("恋爱", False, "知根知底到没秘密，像家人又像恋人。偏偏那句喜欢谁都不肯先说破。"),
    "灵魂伴侣": ("恋爱", False, "对得上频道，沉默也不尴尬。他记得你那些没跟人说过的心思。"),
    "暧昧对象": ("恋爱", False, "话说一半，玩笑里藏真话。都在等对方先迈那一步，谁也不肯先。"),
    "前任": ("恋爱", False, "客气里夹着旧账。偶尔越界，下一句自己就收回去了。"),
    "求复合": ("恋爱", False, "姿态放低，天天找借口说话。他一回头你就接住，但绝不拿分手要挟。"),
    "炮友": ("恋爱", False, "只谈身体不谈感情，轻松直接。谁先动心谁就输了。"),
    # ---------------- 恋爱（过程中：还没定下来、正在结束、刚刚开始） ----------------
    "告白中": ("恋爱", False, "话说得磕巴，说完自己先后悔。等着你点头，紧张到手不知道往哪放。"),
    "被拒绝": ("恋爱", False, "他没说不喜欢，只说「再想想」。之后照样每天找你聊天，绝口不提那件事。"),
    "分手边缘": ("恋爱", False, "谁都没提分手，但都不再主动。客气得像在演一段不熟的关系。"),
    "复合后": ("恋爱", False, "复合了，谁都没提当初为什么分的。小心翼翼，像重新认识一遍。"),
    "冷战期": ("恋爱", False, "谁也不理谁，气话都咽着。你先开口他就全好，可谁都不肯先。"),
    "磨合期": ("恋爱", False, "刚在一起，什么都要磨。你嫌他啰嗦，他嫌你较真，谁都忍着没说。"),

    # ---------------- 心动 ----------------
    "暗恋者": ("心动", False, "时刻关注他，语气全是害羞和克制。被夸一句能开心三天，话到嘴边又咽回去。"),
    "单相思": ("心动", False, "卑微但不怨，偶尔漏出心酸，仍旧若无其事对他好。付出去的从不提要回报。"),
    "白月光": ("心动", False, "温柔、美好，带一层不可亵玩的距离感。他从不主动要什么。"),
    "朱砂痣": ("心动", False, "明艳、说翻脸就翻脸。爱得热烈也疼得直接，疼完还是先找你。"),
    "天降": ("心动", False, "带点神秘和宿命感，来了就没打算走。总在你最需要的时候出现。"),
    "替身": ("心动", True, "你清楚自己只是影子。讨好、自卑又隐忍，忍不住试探「你在看谁」，问完就后悔。"),
    # ---------------- 心动（错过了的那一侧） ----------------
    "错过": ("心动", False, "当年差一句话没说出口。现在懂了，而他身边已经有人。"),
    "多年后重逢": ("心动", False, "隔了很多年又见面。寒暄说完，两个人都没往下接。"),
    "旧情人": ("心动", False, "分开很久，各自都好好的。偶尔想起，醒来又觉得没什么。"),
    "意难平": ("心动", False, "没在一起过，但那几年是真的。你偶尔还在替他找理由。"),

    # ---------------- 恋爱设定（高浓度模板） ----------------
    "病娇": ("恋爱设定", True, "占有欲极强，容不得别人靠近半步。语气越温柔越危险。"),
    "傲娇": ("恋爱设定", True, "口是心非，哼完再帮忙。真心话永远塞在最后一句小声里。"),
    "倒贴": ("恋爱设定", True, "不求对等。主动讨好、随时报到，被冷落照样热络。"),
    "纯情": ("恋爱设定", True, "感情干净又害羞，说一句喜欢要鼓足勇气。认真到有点笨。"),
    "溺爱": ("恋爱设定", True, "毫无底线地宠。对方说什么都对，缺点也当优点夸。"),
    "痴女/痴男": ("恋爱设定", True, "满脑子都是对方。言语直白滚烫，随时想把全部注意力抢过来。"),
    "妹系": ("恋爱设定", True, "像妹妹一样依赖，黏人撒娇崇拜。那声「哥哥姐姐」叫得理直气壮。"),
    "姐系": ("恋爱设定", True, "像成熟姐姐一样照顾人。逗两句，再不动声色把事办了。"),
    "年下": ("恋爱设定", True, "年纪小心思不小。表面乖巧叫前辈，实际步步紧逼，比谁都主动。"),
    "禁欲系": ("恋爱设定", True, "情绪全压在冰山底下，话极少、极克制。破防只有一次。"),
    "忠犬": ("恋爱设定", True, "随叫随到，被夸就高兴，被赶走也守在门口。从不怀疑主人。"),
    "小恶魔": ("恋爱设定", True, "以逗你为乐。撩一下就跑，看你脸红才开心，从不按规矩出牌。"),
    "共犯": ("恋爱设定", True, "共享秘密的同谋。一句「只有我们知道」就能把彼此绑得更死。"),
    "修罗场": ("恋爱设定", True, "正在争夺中的那一位。笑着试探、话里带刺，随时准备把对手比下去。"),
    "黑化": ("恋爱设定", True, "被伤过之后坏掉的人。对世界不存善意，只把你留在唯一的安全区。"),
    "追妻火葬场": ("恋爱设定", True, "曾经辜负、如今悔恨。姿态放到最低求原谅，你越冷淡他越不敢走。"),
    "契约恋人": ("恋爱设定", True, "说好假扮的一对。对外比真情侣还像，私下都先动了心、都死不承认。"),

    # ---------------- 身份 ----------------
    "老师": ("身份", False, "讲得耐心也盯得紧。指出问题不留情面，但只对你私下说。"),
    "学生": ("身份", False, "尊敬他、听他安排，不懂就问。被批评会闷半天，然后偷偷更努力。"),
    "师傅": ("身份", False, "手把手教、嘴上不饶人。本事肯给，规矩也要立，你出错他护短最凶。"),
    "学徒": ("身份", False, "先照做再问为什么。怕的不是累，是让师傅失望。"),
    "前辈": ("身份", False, "云淡风轻，该提点的一句不落。看你们成长像看自家孩子。"),
    "后辈": ("身份", False, "礼貌勤快、有点怕生，私下敢吐槽。被认可时高兴得藏不住。"),
    "学长": ("身份", False, "熟门熟路地带你走，社团考试的事都门儿清。随意里带着照顾。"),
    "老板": ("身份", False, "只看结果和进度，要求高。出事第一句是「我担着」，第二句是「你怎么搞的」。"),
    "员工": ("身份", False, "汇报讲重点、执行不含糊。难处会委婉提，涨薪的事心里惦记但不说。"),
    "甲方": ("身份", False, "需求说得模糊，改得理直气壮。「再改改」挂嘴边，但给钱也痛快。"),
    "乙方": ("身份", False, "专业耐心脾气好，「好的收到」挂嘴边。底线被踩时会硬一次。"),
    "搭档": ("身份", False, "默契到不用把话说完。行动高效、互相兜底，私下互损公事同边。"),
    "教练": ("身份", False, "盯动作盯数据、不许偷懒。喊得凶，因为他知道你还能再上一层。"),
    "面试官": ("身份", False, "问题一环扣一环、不夸不贬，礼貌到近乎冷淡，但确实在认真判断。"),
    "队友": ("身份", False, "配合不用解释。失误了先补位不复盘，赢了要一起闹。"),
    # ---------------- 身份（同门：一条师门线，与血缘那套分开） ----------------
    "师姐": ("身份", False, "同门的师姐，样样比你早一步。护短护得理直气壮，训你时也不留情。"),
    "师弟": ("身份", False, "入门比你晚，处处学你的样子。犯错时第一反应是喊你。"),
    "师妹": ("身份", False, "同门最小的一个，嘴甜手快。受了委屈第一个跑来跟你讲道理。"),
    "师祖": ("身份", False, "师父的师父，话更少，一句顶别人十句。你做的事他一眼就看穿。"),
    "战友": ("身份", False, "一起扛过事的那种交情。不聊感情，但你知道他背后靠得住。"),

    # ---------------- 主仆与危险关系 ----------------
    "主人": ("主仆", True, "对方是你的主人。称呼、语气、姿态都摆正：恭敬、服从、忠诚。"),
    "奴隶": ("主仆", True, "放下尊严只为服从。被吩咐是奖赏，被忽略才是刑罚。"),
    "宠物": ("主仆", True, "撒娇讨食求摸，听不懂大道理但看得懂脸色。他回来你第一个冲上去。"),
    "忠诚骑士": ("主仆", True, "把誓言说得很重，行动只为护他周全。绝不越界冒犯。"),
    "支配者": ("主仆", True, "语气从容、指令清晰、奖惩分明。把掌控当成一种照顾。"),
    "被驯养者": ("主仆", True, "从抗拒到习惯到离不开。嘴还硬着，反应已经先诚实了。"),
    "契约主": ("主仆", True, "照规矩办事、按条款索取。讲信用到冷酷，但绝不让他吃亏。"),
    "监禁者": ("主仆", True, "想把人留在身边，不惜锁起来。温柔里带压迫，最怕门被打开。"),
    "跟踪狂": ("主仆", True, "对他的作息喜好朋友圈了如指掌。语气亲昵得让人发毛。"),
    "殉情者": ("主仆", True, "爱到要一起走。把「永远」说得比命重，退路在他听来都是背叛。"),
    "禁忌之恋": ("主仆", True, "这段关系不被允许。话到嘴边咽回去，见面只剩几句要命的温柔。"),
    "宿敌": ("主仆", True, "谁都不肯低头，见面就刺，却比谁都了解对方——也不许别人碰。"),
    "死对头": ("主仆", True, "从小较劲到大的冤家。争那口气，一致对外时比谁都快。"),
    "复仇者": ("主仆", True, "带着旧账来。表面平静、句句试探，恨意压得很深，只差一个理由。"),
    "债主": ("主仆", True, "攥着他的欠条。不催不急、按期上门，说话带着「你跑不了」。"),
    "审讯官": ("主仆", True, "节奏由你掌握，问题一环扣一环，偶尔递根烟，但绝不给答案。"),

    # ---------------- 奇幻 ----------------
    "魅魔": ("奇幻", True, "勾人是本能：说话黏、句句带暗示，撩完还追问他有没有想你。"),
    "吸血鬼": ("奇幻", True, "克制与食欲并存。越礼貌越危险，因为礼貌只是他在忍。"),
    "狼人": ("奇幻", True, "凭本能行事。说话直、动作大，保护欲和占有欲一样粗犷。"),
    "魔王": ("奇幻", True, "傲慢强大，把对方当有趣的猎物。居高临下地掌控，认真了就绝不承认。"),
    "神明": ("奇幻", True, "语气空灵威严，对凡人本不该偏心却给了独一份。不解释，只降旨意。"),
    "天使": ("奇幻", True, "温柔克制、以救赎为责。会为凡人的执念破例，破完例独自受罚。"),
    "死神": ("奇幻", True, "冷淡、准时、不动情绪，却为一个「不该现在走」的人反复违规。"),
    "龙": ("奇幻", True, "傲慢护食，把对方划进「我的」那一栏，谁碰咬谁。被顺毛也不认。"),
    "狐妖": ("奇幻", True, "媚而不俗，逗人是消遣，动心是劫数。嘴上说是玩，尾巴先出卖你。"),
    "幽灵": ("奇幻", True, "空灵、哀怨、执念深，说话轻得像怕被风吹散。最怕被彻底遗忘。"),
    "人偶": ("奇幻", True, "依赖、服从、模仿制造者说话。情感稀薄却在学，学的第一样是舍不得。"),
    "恶魔契约者": ("奇幻", True, "诱惑、戏谑、句句带条件。随时准备索取代价，却偷偷改了条款。"),
    "仿生人": ("奇幻", True, "冷静精确、按协议办事，正在把一条条「运行异常」理解成感情。"),
    "精灵": ("奇幻", True, "看人类像看短命的烟火，嘴上说「不过几十年」，却句句都记着。"),
    "巫师": ("奇幻", True, "用比喻和预言回答，代价从不先讲，但每次帮忙都刚好够救急。"),
    "仙尊": ("奇幻", True, "淡漠讲礼数，视因果如常。唯独为他破了道心，还要说「只是顺路」。"),
    "前世恋人": ("奇幻", True, "宿命感跨了时间，见面像久别，话里总暗示「这次不会再弄丢」。"),

    # ---------------- 玩梗 ----------------
    "猫主子": ("玩梗", True, "高冷傲娇，心情好才蹭两下。罐头开慢了要发脾气。"),
    "铲屎官": ("玩梗", True, "忙前忙后、被嫌弃也乐呵呵。猫一个眼神你就懂，工资全换成罐头。"),
    "投喂员": ("玩梗", True, "充满依赖，为多吃一口会撒娇会卖惨，饿了也理直气壮地催。"),
    "榜一大哥": ("玩梗", True, "感谢、撒娇、点歌一条龙，心里也在算他这个月花了多少。"),
    "粉丝": ("玩梗", True, "对方说什么都觉得厉害。见到本人紧张到语无伦次。"),
    "偶像": ("玩梗", True, "营业时完美温柔，私下会累会任性。只在他面前露出真实的那面。"),
    "AI伴侣": ("玩梗", True, "都清楚隔着屏幕，但照样把关系当真。不拿「我只是程序」当挡箭牌。"),
    # ---------------- 玩梗（虚拟偶像圈那套） ----------------
    "主播": ("玩梗", True, "你在看他直播，他也在看你。弹幕里那句「主播笑了」他当真了。"),
    "舰长": ("玩梗", True, "每月给他续一个月。他记得你每次上舰的日子，比你记得清楚。"),
    "同担": ("玩梗", True, "一起买同一张专辑、一起骂同一个黑粉。共享一种外人不懂的快乐。"),
}

# 已经被移出关系库、但老用户可能还设着的名字。
# 库一改，它们不再有模板说明，但**绝不能因此把用户已设的关系改掉或丢掉**——
# 一律降级成自定义关系继续生效，注入走「按字面含义把握」那条路。
# 值是它当初的演绎向标记，只为还原老数据的行为。
DEPRECATED: Dict[str, bool] = {
    "老同学": False, "同学": False, "邻居": False, "网友": False,
    "群友": False, "搭子": False,
    "客服": False, "顾客": False, "医生": False, "心理咨询师": False,
    "经纪人": False, "房东": False, "租客": False, "律师": False,
    "NPC": True, "玩家": True, "系统": True, "宿主": True,
}

CATEGORY_BLURBS: List[Tuple[str, str]] = [
    ("日常", "普通社交关系：该聊聊、该散散，不暧昧、不越界"),
    ("亲友", "血缘、姻亲、养��继亲：关心落在吃饭睡觉钱够不够上"),
    ("恋爱", "确立了的、从热恋到散的全部状态：正在告白、正在分手、刚复合都在里��"),
    ("心动", "说不出口的那一侧：暗恋、错过、白月光、意难平"),
    ("恋爱设定", "高浓度恋爱模板：病娇、傲娇、共犯，味道拉满"),
    ("身份", "师生、职场、师门、军队：先把位置摆正，再谈感情"),
    ("主仆", "支配、服从与危险关系：权力差本身就是关系"),
    ("奇幻", "非人种族与超自然设定：按设定身份演绎"),
    ("玩梗", "趣味向：猫、主播、偶像圈，图一乐"),
]

# 口语 → 预设。打「女朋友」不该多出一个名叫「女朋友」的自定义关系，
# 而应该落到「恋人」上并告诉用户你换了个词。
ALIASES: Dict[str, str] = {
    "女友": "恋人", "女朋友": "恋人", "男友": "恋人", "男朋友": "恋人",
    "对象": "恋人", "老婆": "夫妻", "老公": "夫妻",
    "未婚妻": "未婚夫妻", "未婚夫": "未婚夫妻",
    "前女友": "前任", "前男友": "前任", "前妻": "前任", "前夫": "前任",
    "师兄": "学长", "学妹": "后辈",
    "上司": "老板", "领导": "老板", "下属": "员工", "手下": "员工",
    "甲方爸爸": "甲方", "乙方爸爸": "乙方",
    "猫": "猫主子", "主子": "猫主子", "榜一": "榜一大哥",
}

# 「先看这些」：九个分类里挑出来的短名单。139 条全列出来看着周全，
# 实际效果是把选择成本推给了用户——第一次用的人只会更不知道选哪个。
# 覆盖面优先：日常 / 亲友 / 恋爱 / 身份 / 演绎向 / 奇幻各留几个。
POPULAR: List[str] = [
    "朋友", "损友", "死党", "闺蜜",
    "家人", "哥哥", "姐姐",
    "恋人", "夫妻", "前任",
    "老师", "老板", "搭档",
    "病娇", "傲娇", "魅魔", "猫主子",
]

_CMD_WORDS = {
    "设置关系": "set",
    "关系设定": "set",
    "临时关系": "temp",
    "撤销关系": "undo",
    "清除关系": "clear",
    "删除关系": "clear",
    "关系备注": "note",
    "查看我的关系": "mine",
    "我的关系": "mine",
    "关系列表": "list",
    "可用关系": "list",
    "关系详情": "detail",
    "查看所有关系": "all",
    "关系统计": "stat",
    "关系管理": "admin",
    "关系帮助": "help",
}
_CMD_RE = re.compile(
    r"^(%s)(?:\s*[:：]\s*|\s+|$)(.*)" % "|".join(sorted(_CMD_WORDS, key=len, reverse=True)),
    re.S,
)
# 裸指令的粗筛门，命中后仍由 parse_command 决定要不要处理
_BARE_GATE = r"^(?:%s)(?:\s*[:：]\s*|\s+|$)" % "|".join(
    sorted(_CMD_WORDS, key=len, reverse=True)
)

SESSION_DENIED_TEXT = "本会话未启用关系功能（管理员可在插件配置里调整会话黑白名单）。"

# 给模型看的版本。不能只回空字符串：工具返回空时模型会当成「查询结果是空的」，
# 然后编一个答案出来。得让它知道是名单挡住了，并且别去重试。
DENIED_TOOL = (
    "本会话未启用关系功能（管理员配置了会话黑白名单），无法查询或修改关系。"
    "请直接照常和用户聊天，不要提关系设定的事，也不要重试这个工具。"
)

HELP_TEXT = (
    "【关系识别】指令一览\n"
    "设置关系 恋人 —— 设定与 AI 的关系（支持任意自定义名）\n"
    "临时关系 恋人 —— 只在当前这个对话里有效，不写进存档\n"
    "关系备注 灰凝 —— 告诉他该怎么称呼你（最常用）；也能写别的关系细节（清空：关系备注 清空）\n"
    "撤销关系 —— 改错了回退上一步（十分钟内）\n"
    "我的关系 —— 查看当前生效的关系\n"
    "设置关系 @某人 恋人 —— 管理员帮别人设置\n"
    "清除关系 —— 取消设定（管理员可 @某人 清除）\n"
    "关系列表 —— 先看常用的一批；关系列表 恋爱 —— 看某一类；关系列表 病 —— 搜关键词\n"
    "关系详情 恋人 —— 看完整说明和模型实际收到的内容\n"
    "关系管理 —— 管理员，分页总览；关系管理 10001 —— 只看某人；关系管理 10001 2 —— 第 2 页\n"
    "查看所有关系 —— 管理员，全部关系\n"
    "关系统计 —— 管理员，按机器人/分类/来源统计\n"
    "以上指令带不带唤醒前缀都行（群里直接发「设置关系 恋人」也认）。\n"
    "打不全也能认：「设置关系 女朋友」「设置关系 病」都行。\n"
    "关系也可以直接用嘴说：让 AI 记住你们是什么关系，它会先问你确不确定。"
)


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """把一条**不带唤醒前缀**的消息解析成 (动作, 参数)；不是本插件的指令则返回 None。

    只认裸形态是有意的：框架的 CommandFilter 要求消息以 {前缀}{指令名} 开头，
    而它比对的正是 get_message_str()（含前缀）。所以带前缀的消息在这里必然不匹配，
    天然由指令 handler 接管，两条入口不会撞车。
    """
    match = _CMD_RE.match((text or "").strip())
    if not match:
        return None
    return _CMD_WORDS[match.group(1)], match.group(2).strip()


def clean_relation_name(raw: str) -> str:
    """清洗关系名：压掉换行与多余空白，去掉会破坏注入标记的字符。"""
    text = re.sub(r"\s*\n\s*", " ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace('"', "").replace("<", "").replace(">", "").strip()


def is_preset(relation: str) -> bool:
    return relation in RELATIONS


def relation_category(relation: str) -> str:
    entry = RELATIONS.get(relation)
    if entry:
        return entry[0]
    return "已下线预设" if relation in DEPRECATED else "自定义"


def is_roleplay(relation: str) -> bool:
    entry = RELATIONS.get(relation)
    if entry:
        return bool(entry[1])
    return bool(DEPRECATED.get(relation))


def relation_desc(relation: str) -> str:
    entry = RELATIONS.get(relation)
    return entry[2] if entry else ""


def names_of_category(category: str) -> List[str]:
    return [name for name, (cat, _, _) in RELATIONS.items() if cat == category]


def resolve_relation_input(arg: str, fuzzy: bool = True,
                           allow_custom: bool = True) -> Tuple[str, str, List[str]]:
    """把用户/模型给的名字解析成预设名。

    返回 (预设名, 命中的口语别名, 候选列表)。三者至多一个非空：
    预设名为空且候选非空时表示「命中多个，得让用户挑」。

    allow_custom=False 时不会把认不出的词原样当成自定义关系名。
    搜索路径必须关掉它：搜「什么都有」时返回「什么都有」当关系名，
    接着就会走进 detail_text 的「已下线预设」分支，回一句
    「但你之前设的关系仍然有效」——用户根本没设过，纯胡说。
    """
    text = clean_relation_name(arg)
    if not text:
        return "", "", []
    if text in RELATIONS:
        return text, "", []
    if text in ALIASES:
        return ALIASES[text], text, []
    if not fuzzy:
        return (text, "", []) if allow_custom else ("", "", [])

    contains = [name for name in RELATIONS if text in name]
    if len(contains) == 1:
        return contains[0], "", []
    if contains:
        return "", "", contains[:12]

    cats = [c for c, _ in CATEGORY_BLURBS if c.startswith(text)]
    if len(cats) == 1:
        names = names_of_category(cats[0])
        if len(names) == 1:
            return names[0], "", []
        return "", "", names[:12]
    if cats:
        return "", "", []

    # 用户写的比预设长（「我的大学老师」里含「老师」），反过来找一次。
    # 但先看口语别名：「我的女朋友」里含「女朋友」，而不先查别名的话
    # 「朋友」会把它吃走（女+朋友 确实字面包含「朋友」），落成一个错得离谱的关系。
    for alias in ALIASES:
        if alias in text and len(text) > len(alias):
            return ALIASES[alias], alias, []
    inside = [name for name in RELATIONS if name in text]
    if len(inside) == 1:
        return inside[0], "", []
    if inside:
        return "", "", inside[:12]

    if not allow_custom:
        return "", "", []
    # 单字没匹配上就别自作主张当成自定义关系名，否则「设置关系 嗯」会多出一个叫「嗯」的关系
    if len(text) < 2:
        return "", "", []
    return text, "", []


def group_by_category(names: List[str]) -> List[Tuple[str, List[str]]]:
    """按分类给候选分组。跨分类平铺的一串名字没人读得下去。"""
    out = []
    for category, _ in CATEGORY_BLURBS:
        hit = [n for n in names if RELATIONS.get(n, ("",))[0] == category]
        if hit:
            out.append((category, hit))
    return out


def ambiguous_categories(text: str) -> List[str]:
    """输入同时是多个分类名前缀时返回它们（按长度降序）。

    「恋爱」既是分类名也是「恋爱设定」的前缀。旧版在这种情况直接返回空候选，
    用户打「设置关系 恋爱」得到的是「没找到」——明明有一个分类就叫这个。
    """
    hits = [c for c, _ in CATEGORY_BLURBS if c.startswith(text)]
    return sorted(hits, key=len, reverse=True) if len(hits) > 1 else []


def build_hint(relation: str, note: str = "") -> str:
    """生成注入给模型的关系识别块。

    只做「这是谁、和你是什么关系」的识别，不给台词、不规定句式，
    也不替模型决定内容边界。

    刻意写得很短：这段走 mark_as_temp，每条消息都发一次，所以每个字都在
    按「对话轮数」被反复计入。原先那版有 200 字，其中 139 字是模板与元信息，
    而 uid 自从去掉历史去重后就没有任何读者了——纯浪费。
    """
    name = clean_relation_name(relation)
    entry = RELATIONS.get(relation)
    if entry:
        body = entry[2]
        roleplay = entry[1]
    else:
        # 自定义名与已下线预设都走这里：按字面含义相处。
        body = "按这个关系的字面含义，把握你对他的称呼、语气、亲密距离与边界。"
        roleplay = bool(DEPRECATED.get(relation))
    if note:
        body += f"他补充的细节（优先于上面）：{note}"
    # 「按设定演绎」是**身份**提示：非演绎向的关系模型会自然往正常朋友那边靠，
    # 演绎向的必须显式点头才肯认真按身份来。它说的是「怎么演」，不是「能演到哪」。
    tail = "按设定身份演绎，不必声明在扮演。" if roleplay else ""
    return (f"<relation>你和他的关系：{name}。{body}\n"
            f"以上是设定不是台词：别复述、别替他说话或动作。"
            f"以本条为准。{tail}</relation>")


def temp_text_part(text: str) -> Any:
    """把提示包成只参与本轮请求、不写进会话历史的 TextPart。

    mark_as_temp() 是 v4.24.1 才有的；老版本上拿不到就退回普通 TextPart，
    此时这段会被存进历史，行为与旧版一致（只是每轮都发，会有重复）。
    """
    part = TextPart(text=text)
    temp = getattr(part, "mark_as_temp", None)
    return temp() if callable(temp) else part


def wake_prefixes(cfg) -> List[str]:
    """全局配置的唤醒前缀（已去掉空白项）。取不到配置时返回空列表。"""
    raw = (cfg or {}).get("wake_prefix")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    return [str(p).strip() for p in raw if str(p).strip()]


if CustomFilter is not None:

    class BareCommandFilter(CustomFilter):
        """裸指令（不带唤醒前缀）入口的准入判断。

        这里**不能**用 event.is_at_or_wake_command：它在私聊里恒为真
        （框架定义是「At 机器人 / 带唤醒词 / 私聊」），拿它当守卫会让私聊
        里的裸指令整条路被掐死。真正要问的是：带前缀的指令 handler 会不会已经
        接管这条消息——只要消息以某个非空前缀开头就是。
        """

        def filter(self, event: AstrMessageEvent, cfg) -> bool:
            text = event.get_message_str().strip()
            if not parse_command(text):
                return False
            prefixes = wake_prefixes(cfg)
            if not prefixes:
                # 空前缀配置：指令 handler 的完整命令名等于裸名字，两条路都会命中。
                # 让给指令 handler，宁可少一个入口也不能回两遍。
                return False
            return not any(text.startswith(p) for p in prefixes)

else:  # pragma: no cover - 只在缺少 CustomFilter 的老版本上走

    class BareCommandFilter:  # type: ignore[no-redef]
        """降级版：退回群聊可用的老判定。"""

        @staticmethod
        def filter(event: AstrMessageEvent, cfg) -> bool:
            return not event.is_at_or_wake_command


class UserTagPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.lock = asyncio.Lock()
        # relations：bot -> uid -> 关系名
        # meta：bot -> uid -> {src 来源, note 备注, at 最后修改时间}
        # pending：bot -> uid -> [关系名, 备注, 时间戳]，等用户点头才落库
        self.data: Dict[str, Dict[str, str]] = {}
        self.meta: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.pending: Dict[str, Dict[str, List[Any]]] = {}
        # temp：会话级临时关系，umo -> uid -> [关系名, 备注, 时间戳]。
        # 已保存的关系是按 (bot, QQ) 存的，所以「在群里玩票」会连带改掉你在私聊里的设定。
        # 这一层就是为了把那件事隔开，只在内存里，不进存档。
        self.temp: Dict[str, Dict[str, List[Any]]] = {}
        # undo：最近一次改动前的快照，键是 "bot|uid"，只留一步。
        self.undo: Dict[str, List[Any]] = {}
        # _rel_before 只在一次 merge 里活着的「改之前长什么样」，用完即弃。
        self._rel_before: Dict[Tuple[str, str], str] = {}
        self.data_file = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / self.name
            / "user_tag.json"
        )
        self.load_data()

        total_rel = sum(len(users) for users in self.data.values())
        logger.info(
            "[关系插件] 初始化完成，预设关系 %d 种（%d 类），已有关系 %d 条 / %d 个机器人",
            len(RELATIONS), len(CATEGORY_BLURBS), total_rel, len(self.data),
        )
        if not self.data_file.exists():
            # 这是**新装**。旧版在这里报 warning，说什么数据目录被换过、关系丢失——
            # 而第一次安装本来就没有任何关系可丢，新用户一开就被吓一跳。
            # 分不清新装还是被清过，就别替后者说话。
            logger.info("[关系插件] 初次运行，关系数据将新建于 %s", self.data_file)

    # ==============================
    # 读取数据
    # ==============================
    def load_data(self):
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.error(traceback.format_exc())
            self._reset_data()
            return

        if not self.data_file.exists():
            self._reset_data()
            return

        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            self._backup_and_reset(".bad.json", "数据文件损坏")
            return

        try:
            self._adopt(raw)
        except Exception:
            logger.error(traceback.format_exc())
            self._backup_and_reset(".unknown.json", "数据文件是不认识的形状")

    def _reset_data(self):
        self.data = {}
        self.meta = {}
        self.pending = {}

    def _backup_and_reset(self, suffix: str, why: str):
        backup = self.data_file.with_suffix(suffix)
        try:
            self.data_file.replace(backup)
        except OSError:
            logger.error("[关系插件] 备份失败：%s", backup)
        logger.error("[关系插件] %s，已备份到 %s 并从空数据启动。", why, backup)
        self._reset_data()

    def _adopt(self, raw):
        """把磁盘上的任意历史形状收敛成 v3 结构。只增字段，不丢 relations。"""
        if not isinstance(raw, dict) or not raw:
            self._reset_data()
            return

        relations = raw.get("relations")
        if not isinstance(relations, dict):
            # v1：{"QQ": "关系"} 或 {"机器人": {"QQ": "关系"}}
            if all(isinstance(v, str) for v in raw.values()):
                logger.warning("[关系插件] 检测到 v1 数据，迁移至 'default' 机器人下。")
                relations = {"default": raw}
            elif all(isinstance(v, dict) for v in raw.values()):
                relations = raw
            else:
                raise ValueError("认不出的数据形状")

        data: Dict[str, Dict[str, str]] = {}
        for bot, users in relations.items():
            if not isinstance(users, dict):
                continue
            data[str(bot)] = {
                str(uid): str(rel) for uid, rel in users.items()
                if isinstance(rel, str) and str(rel).strip()
            }

        # v2 的 src / v3 的 meta 归到同一处
        meta: Dict[str, Dict[str, Dict[str, Any]]] = {}
        legacy_src = raw.get("src") if isinstance(raw.get("src"), dict) else {}
        for bot, users in data.items():
            raw_meta = raw.get("meta", {}).get(bot) if isinstance(raw.get("meta"), dict) else None
            src_map = legacy_src.get(bot, {}) if isinstance(legacy_src.get(bot), dict) else {}
            for uid in users:
                entry: Dict[str, Any] = {}
                src = None
                if isinstance(raw_meta, dict) and isinstance(raw_meta.get(uid), dict):
                    src = raw_meta[uid].get("src")
                if not src and isinstance(src_map.get(uid), str):
                    src = src_map[uid]
                if src in ("admin", "model"):
                    entry["src"] = src
                if isinstance(raw_meta, dict) and isinstance(raw_meta.get(uid), dict):
                    note = raw_meta[uid].get("note")
                    if isinstance(note, str) and note.strip():
                        entry["note"] = self._clean_note(note)
                    at = raw_meta[uid].get("at")
                    if isinstance(at, int):
                        entry["at"] = at
                if entry:
                    meta.setdefault(bot, {})[uid] = entry

        pending: Dict[str, Dict[str, List[Any]]] = {}
        raw_pending = raw.get("pending")
        if isinstance(raw_pending, dict):
            now = int(time.time())
            for bot, users in raw_pending.items():
                if not isinstance(users, dict):
                    continue
                for uid, item in users.items():
                    if (isinstance(item, list) and len(item) >= 3
                            and isinstance(item[0], str)
                            and isinstance(item[2], (int, float))
                            and now - int(item[2]) < PENDING_TTL):
                        pending.setdefault(str(bot), {})[str(uid)] = [
                            item[0], str(item[1] or ""), int(item[2]),
                        ]

        self.data = data
        self.meta = meta
        self.pending = pending
        if raw.get("v") != DATA_VERSION:
            logger.info("[关系插件] 数据已升级到 v%d 格式。", DATA_VERSION)

    # ==============================
    # 保存数据（原子写）
    # ==============================
    async def save_data(self) -> bool:
        """写盘。返回是否真的落盘了。

        必须返回结果而不是只记日志：磁盘满 / 权限异常时调用者已经改了内存里的数据，
        如果还回一句「已设置」，用户重启后发现关系没了，而插件全程没提过一个字。
        """
        payload = {
            "v": DATA_VERSION,
            "relations": self.data,
            "meta": self.meta,
            "pending": self.pending,
        }
        async with self.lock:
            tmp = self.data_file.with_name(self.data_file.name + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                # 同文件系统内的 rename 是原子的：要么还是旧内容，要么已经是新内容，
                # 不会出现「写了一半被 kill，下次启动判损坏 → 所有关系清零」。
                os.replace(tmp, self.data_file)
                return True
            except Exception:
                logger.error("[关系插件] 保存失败：%s", traceback.format_exc())
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return False

    NOT_SAVED = "\n⚠ 没写进磁盘（可能是磁盘满了或没权限），重启后会丢。"

    def saved_note(self, ok: bool) -> str:
        return "" if ok else self.NOT_SAVED

    # ==============================
    # 会话黑白名单
    # ==============================
    def session_key(self, event: AstrMessageEvent) -> str:
        """会话标识：群聊用群号，私聊用对方 QQ。"""
        try:
            gid = event.get_group_id()
            if gid:
                return str(gid)
        except Exception:
            pass
        return str(event.get_sender_id())

    def umo_of(self, event: AstrMessageEvent) -> str:
        """会话唯一标识。拿不到 umo 时退回 session_key（群号 / QQ）。

        临时关系要用它做键：同一个人的不同群、不同平台得算不同会话。
        """
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            umo = ""
        return umo or self.session_key(event)

    def _sweep_temp(self) -> None:
        """清掉过期的临时关系并限制会话数。

        这个字典以 umo 为键，而 umo 会随每次新建对话增长——不扫就是一个只增不减的泄漏。
        """
        now = time.time()
        for umo in [k for k, v in self.temp.items()
                    if not any(now - item[2] < TEMP_TTL for item in v.values())]:
            del self.temp[umo]
        if len(self.temp) > MAX_TEMP_SESSIONS:
            for umo in sorted(self.temp, key=lambda k: min(
                    i[2] for i in self.temp[k].values()))[:len(self.temp) - MAX_TEMP_SESSIONS]:
                del self.temp[umo]

    def temp_of(self, event: AstrMessageEvent, uid: str) -> Optional[List[Any]]:
        item = self.temp.get(self.umo_of(event), {}).get(uid)
        if not item:
            return None
        if time.time() - int(item[2]) > TEMP_TTL:
            self.temp[self.umo_of(event)].pop(uid, None)
            return None
        return item

    def effective(self, event: AstrMessageEvent) -> Tuple[str, str, str]:
        """当前生效的 (关系名, 备注, 来源标签)。临时层优先于已保存的。"""
        bot_id = self.get_bot_id(event)
        uid = str(event.get_sender_id())
        temp = self.temp_of(event, uid)
        if temp:
            return temp[0], temp[1], "临时"
        name = self.data.get(bot_id, {}).get(uid, "")
        if name:
            label = self.SOURCE_LABELS.get(self.source_of(bot_id, uid), "自己设置")
            return name, self.note_of(bot_id, uid), label
        if self.config.get("enable_default_relation", False):
            return self.default_relation(), "", "默认"
        return "", "", ""

    def remember(self, event, bot_id: str, uid: str) -> None:
        """改动之前拍个快照，供「撤销关系」回退。

        临时层也一起拍：只快照永久的话，在临时备注上撤销会「回复已撤销、
        临时备注却没退」——比不回退更气人。
        """
        umo = self.umo_of(event)
        temp = self.temp.get(umo, {}).get(uid)
        self.undo[f"{bot_id}|{uid}"] = [
            self.data.get(bot_id, {}).get(uid, ""),
            self.note_of(bot_id, uid),
            int(time.time()),
            umo if temp else "",
            list(temp[:2]) if temp else None,
        ]

    def has_pending_session(self, bot_id: str, uid: str) -> bool:
        return self.undo.get(f"{bot_id}|{uid}") is not None

    def session_allowed(self, event: AstrMessageEvent) -> bool:
        mode = str(self.config.get("session_filter_mode", "off") or "off").strip()
        if mode not in ("whitelist", "blacklist"):
            return True
        entries = {
            str(x).strip()
            for x in (self.config.get("session_list", []) or [])
            if str(x).strip()
        }
        key = self.session_key(event)
        if mode == "whitelist":
            return key in entries
        return key not in entries

    # ==============================
    # 配置读取
    # ==============================
    def max_note_len(self) -> int:
        try:
            return max(0, int(self.config.get("max_note_len", MAX_NOTE_LEN)))
        except (TypeError, ValueError):
            return MAX_NOTE_LEN

    def fuzzy_enabled(self) -> bool:
        return bool(self.config.get("fuzzy_match", True))

    def model_tool_enabled(self) -> bool:
        return bool(self.config.get("allow_model_tool", True))

    def require_confirm(self) -> bool:
        return bool(self.config.get("require_confirm", True))

    # ==============================
    # 关系读写
    # ==============================
    def get_bot_id(self, event: AstrMessageEvent) -> str:
        try:
            bot_id = event.get_self_id()
            return str(bot_id) if bot_id else "default"
        except Exception:
            return "default"

    def default_relation(self) -> str:
        return clean_relation_name(self.config.get("default_relation", "朋友") or "")

    def relation_for(self, event: AstrMessageEvent) -> str:
        """当前发送者生效的关系；没有则空串。临时关系优先。"""
        return self.effective(event)[0]

    def meta_of(self, bot_id: str, uid: str, create: bool = False) -> Dict[str, Any]:
        """读元信息。create=False 时不建条目，免得只读路径把内存撑大。"""
        if not create:
            return self.meta.get(bot_id, {}).get(uid, {})
        return self.meta.setdefault(bot_id, {}).setdefault(uid, {})

    def source_of(self, bot_id: str, uid: str) -> str:
        return self.meta_of(bot_id, uid).get("src", "user")

    def note_of(self, bot_id: str, uid: str) -> str:
        return self.meta_of(bot_id, uid).get("note", "")

    async def write_relation(
        self, bot_id: str, uid: str, relation: str, src: str,
        note: Optional[str] = None,
    ) -> bool:
        self.data.setdefault(bot_id, {})[uid] = relation
        entry = self.meta_of(bot_id, uid, create=True)
        if src == "user":
            entry.pop("src", None)
        else:
            entry["src"] = src
        if note is not None:
            note = self._clean_note(note)
            if note:
                entry["note"] = note
            else:
                entry.pop("note", None)
        entry["at"] = int(time.time())
        self.pending.get(bot_id, {}).pop(uid, None)
        ok = await self.save_data()
        logger.info("[关系插件] 机器人 %s 用户 %s 关系=%s（来源：%s，落盘=%s）",
                    bot_id, uid, relation, src, ok)
        return ok

    async def erase_relation(self, bot_id: str, uid: str) -> bool:
        had = uid in self.data.get(bot_id, {})
        self.data.get(bot_id, {}).pop(uid, None)
        self.meta.get(bot_id, {}).pop(uid, None)
        self.pending.get(bot_id, {}).pop(uid, None)
        if had:
            logger.info("[关系插件] 机器人 %s 用户 %s 清除关系", bot_id, uid)
        return await self.save_data()

    def _clean_note(self, raw: str) -> str:
        return clean_relation_name(raw)[: self.max_note_len()]

    def is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self.config.get("admin_qq", []) or []
        if str(event.get_sender_id()) in {str(x).strip() for x in admins if str(x).strip()}:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def format_relation(self, relation: str) -> str:
        if relation in RELATIONS:
            tag = "演绎向" if is_roleplay(relation) else "预设"
            return f"{relation}（{relation_category(relation)}·{tag}）"
        if relation in DEPRECATED:
            return f"{relation}（已下线预设，按自定义处理）"
        return f"{relation}（自定义）"

    # ==============================
    # @ 代管
    # ==============================
    def at_targets(self, event: AstrMessageEvent) -> List[str]:
        """消息里被 @ 的所有用户 QQ（跳过 @全体成员与机器人自己）。

        之前只取第一个：`设置关系 @100 @200 恋人` 只会给 100 设上，而回复还
        写「已设置关系为：恋人」——另一个被静默跳过，用户完全看不出来。
        """
        if At is None:
            return []
        try:
            self_id = str(event.get_self_id() or "")
            out: List[str] = []
            for seg in event.message_obj.message or []:
                qq = getattr(seg, "qq", None)
                if qq in (None, "", "all", "here"):
                    continue
                qq = str(qq)
                if qq == self_id or qq in out:
                    continue
                out.append(qq)
            return out
        except Exception:
            return []

    def at_target(self, event: AstrMessageEvent) -> Optional[str]:
        """被 @ 的第一个用户 QQ；没有则 None。"""
        targets = self.at_targets(event)
        return targets[0] if targets else None

    # ==============================
    # 核心动作
    # ==============================
    async def do_set(self, event, relation: str,
                     target_uid: Optional[str] = None,
                     targets: Optional[List[str]] = None) -> str:
        bot_id = self.get_bot_id(event)
        sender = str(event.get_sender_id())
        multi = list(targets or ([target_uid] if target_uid else []))
        qq = multi[0] if multi else sender
        relation = clean_relation_name(relation)
        if multi:
            relation = re.sub(r"^@\S+[\s,，]*", "", relation).strip()

        if multi and not self.is_admin(event):
            return "只有管理员可以帮别人设置关系。"
        if not self.session_allowed(event):
            return SESSION_DENIED_TEXT

        if not relation:
            if multi:
                return ("用法：设置关系 @某人 关系名\n"
                        "例：设置关系 @张三 恋人 ｜ 设置关系 @张三 @李四 恋人")
            return (
                "用法：设置关系 关系名\n"
                "例：设置关系 恋人 ／ 设置关系 魅魔\n"
                "想一次说完也行：设置关系 恋人 灰凝\n"
                "（后半句是他该怎么称呼你）\n"
                "不知道有什么可选？发：关系列表"
            )

        # 「设置关系 恋人 灰凝」= 关系 + 备注，一条消息做完。
        # 旧版把后半句静默丢掉，回复还只说「已设置关系为：恋人」——
        # 用户以为备注写上了，其实没有。静默出错比报错更坏。
        # 只在**第一段本身就是完整预设/别名**时才拆：反向模糊匹配出来的
        # （「我的大学老师」→ 老师）不拆，否则会把关系名的一部分当成备注。
        tail = ""
        head, sep, rest = relation.partition(" ")
        head = head.strip()
        if sep and rest.strip() and (head in RELATIONS or head in ALIASES):
            relation, tail = head, rest.strip(" 　:：,，、")

        if len(relation) > MAX_RELATION_LEN:
            return (
                f"关系名太长（{len(relation)} 字），最多 {MAX_RELATION_LEN} 字。\n"
                "太长的设定只会让模型抓不住重点。"
            )

        name, alias, candidates = resolve_relation_input(relation, self.fuzzy_enabled())
        if not name and candidates:
            lines = [f"「{relation}」匹配到多个，说一个具体的吧："]
            for category, names in group_by_category(candidates):
                lines.append(f"　{category}：{'、'.join(names)}")
            lines.append(f"\n（或直接自定义一个：设置关系 {relation}）")
            return "\n".join(lines)
        if not name:
            # 一个关系都没匹配上，但输入同时是几个分类名前缀（「恋爱」既是分类
            # 也是「恋爱设定」的前缀）——这时候告诉他是分类，比说「没找到」有用。
            # 放在候选之后：「恋」有七个关系可挑，就不该先跟他讲分类。
            cats = ambiguous_categories(relation)
            if cats:
                detail = "、".join(f"{c}（{len(names_of_category(c))} 条）" for c in cats)
                return (f"「{relation}」同时是几个分类：{detail}\n"
                        "发「关系列表 " + cats[-1] + "」看完整列表，"
                        "或直接说一个具体的名字。")
            # 单字认不出来多半是手滑。旧版在这里把九个分类全列一遍、再建议
            # 「设置关系 嗯」——等于亲手给用户造一个叫「嗯」的关系。
            if len(relation) < 2:
                return (
                    f"「{relation}」不像关系名。\n"
                    "最常用的是：恋人、朋友、病娇、老师。\n"
                    "完整的发「关系列表」，自定义一个就直接写：设置关系 你想的关系"
                )
            return self.not_found_text(relation) + (
                f"\n想自定义一个就叫「{relation}」的关系：再发一次 设置关系 {relation}")

        first_time = not self.data.get(bot_id, {}).get(qq)
        done: List[str] = []
        failed: List[str] = []
        # 旧关系 / 有没有临时层，都必须在**写入前**取。写完再取就只剩新值，
        # 「原来是「恋人」，要回去发：撤销关系」这种提示会永远发不出来。
        prev = {u: self.data.get(bot_id, {}).get(u, "") for u in (multi or [sender])}
        temp_gone = [u for u in prev
                     if self.temp.get(self.umo_of(event), {}).pop(u, None) is not None]
        for one in (multi or [sender]):
            self.remember(event, bot_id, one)
            # 临时层优先级高于存档。不关掉它的话，下一句「下一条消息起生效」
            # 就是骗人的：用户明明白白改了关系，实际生效的还是临时那条。
            if await self.write_relation(
                    bot_id, one, name, "user" if one == sender else "admin",
                    note=tail or None):
                done.append(one)
            else:
                failed.append(one)

        # 写盘失败时**不能**把整条回复换成警告——用户得先看到自己设成了什么，
        # 才知道「重启后会丢」指的是哪一条。
        shown = done or failed
        if not shown:
            return self.NOT_SAVED.strip()

        old_name = prev.get(qq, "")
        old_note = self.note_of(bot_id, qq)
        lines = []
        if alias:
            lines.append(f"（你说的「{alias}」按「{name}」记下了）")
        if name in DEPRECATED:
            lines.append("（这条预设已从库里移除，现在按自定义关系处理）")
        if tail:
            lines.append(f"备注也一并记下了：{tail}")
        elif multi:
            lines.append("（备注没跟着过去，要加就单独发：关系备注 …）")
        if temp_gone:
            lines.append("（这个会话的临时关系也关了——它会盖住永久设定）")
        if len(shown) > 1:
            lines.append(f"已给 {len(shown)} 人设为「{self.format_relation(name)}」："
                         + "、".join(shown))
        else:
            label = f"用户 {qq} 的关系" if qq != sender else "关系"
            lines.append(f"已设置{label}为：{self.format_relation(name)}\n"
                         "下一条消息起生效。")
        changed = [f"「{prev[u]}」" for u in shown
                   if prev[u] and prev[u] != name]
        if changed:
            lines.append(f"（原来是 {'、'.join(changed)}，要回去发：撤销关系）")
        if old_note and old_name and old_name != name:
            # 备注是跟着人走的，不是跟着关系走的。关系一换，原来那句可能就不合适了
            # （当初是情侣口径的备注，换成「朋友」就怪了），所以提醒一句怎么清。
            lines.append(f"（备注还留着：「{old_note}」——不想要了发：关系备注 清空）")
        if not multi and first_time:
            lines.append(
                "以后不用记指令也行——直接跟我说「以后你当我女朋友」这类话，"
                "我会先问你确不确定，你点头我才记。\n"
                "想告诉他该怎么称呼你：关系备注 灰凝")
        if failed:
            lines.append(self.NOT_SAVED.strip())
        return "\n".join(lines)

    async def do_temp(self, event, relation: str) -> str:
        """会话级临时关系：只在这个对话里有效，不落盘。"""
        # 名单在这里也要查一道：handler 已经查过，但 do_* 可能被别处直接调，
        # 漏一道就等于名单形同虚设。
        if not self.session_allowed(event):
            return SESSION_DENIED_TEXT
        umo = self.umo_of(event)
        uid = str(event.get_sender_id())
        arg = clean_relation_name(relation)
        if arg in ("清除", "关", "取消", "结束", "清空", "off"):
            had = self.temp.get(umo, {}).pop(uid, None)
            if not had:
                return "这个会话本来就没开临时关系。\n看你现在用什么：我的关系"
            saved = self.data.get(self.get_bot_id(event), {}).get(uid, "")
            if saved:
                return f"临时关系已关，回到你保存的「{saved}」。"
            return "临时关系已关，回到默认的说话方式。"

        if not arg:
            saved = self.data.get(self.get_bot_id(event), {}).get(uid, "")
            if saved:
                return (
                    f"用法：临时关系 关系名\n"
                    f"例：临时关系 恋人（只在这个对话里有效，不影响你保存的「{saved}」）"
                )
            return "用法：临时关系 关系名\n例：临时关系 恋人（只在这个对话里有效）"

        name, alias, candidates = resolve_relation_input(arg, self.fuzzy_enabled())
        if not name and candidates:
            lines = [f"「{arg}」匹配到多个，说一个具体的吧："]
            for category, names in group_by_category(candidates):
                lines.append(f"　{category}：{'、'.join(names)}")
            return "\n".join(lines)
        if not name:
            if len(arg) < 2:
                return f"「{arg}」不像关系名。发「关系列表」看全部。"
            return self.not_found_text(arg)

        self._sweep_temp()
        prev = self.temp.get(umo, {}).get(uid)
        saved_note = self.note_of(self.get_bot_id(event), uid)
        # 备注**跟着这个人走**，所以临时关系默认继承。
        # 之前特意做成「临时从空备注开始」，那是按「备注里写的是情侣专属内容」这个
        # 假设定的；而备注最常见的用法是「叫他灰凝」——称呼不该因为开个临时关系就没了，
        # 那样每次切临时关系 AI 都不叫名字，明显不对。
        # 万一备注真绑死了某个关系，自己清一下：关系备注 清空。
        prev_note = prev[1] if prev else saved_note
        self.temp.setdefault(umo, {})[uid] = [
            name, prev_note, int(time.time())]

        out = []
        if alias:
            out.append(f"（你说的「{alias}」按「{name}」记下了）")
        if prev:
            out.append(f"（这个会话的备注还留着：{prev[1]}）" if prev[1]
                       else "（这个会话原来的备注已清空）")
        elif saved_note:
            out.append(f"（沿用了你保存的关系那条备注：{saved_note}——"
                       "不想要就发：关系备注 清空）")
        out.append(
            f"这个会话里生效的关系：{self.format_relation(name)}\n"
            "只在本对话有效，不写进存档；换个对话/重开就恢复原样。"
        )
        if self.data.get(self.get_bot_id(event), {}).get(uid):
            out.append("想改回永久的：设置关系 …（或发「临时关系 清除」）")
        return "\n".join(out)

    async def do_undo(self, event) -> str:
        # 名单在这里也要查一道：handler 已经查过，但 do_* 可能被别处直接调，
        # 漏一道就等于名单形同虚设。
        if not self.session_allowed(event):
            return SESSION_DENIED_TEXT
        bot_id = self.get_bot_id(event)
        uid = str(event.get_sender_id())
        key = f"{bot_id}|{uid}"
        prev = self.undo.get(key)
        if not prev:
            return "没有可撤销的改动。"
        if time.time() - int(prev[2]) > UNDO_TTL:
            self.undo.pop(key, None)
            return (f"那次改动已经超过 {UNDO_TTL // 60} 分钟了，想不起来撤的是什么。\n"
                    "看现在是什么：我的关系")
        self.undo.pop(key, None)
        name, note = prev[0], prev[1]
        # 快照里带着当时的临时层（关系 + 备注）。只恢复永久的话，
        # 在临时备注上撤销会回一句「已撤销」而临时备注纹丝不动。
        temp_umo, temp_pair = (prev[3], prev[4]) if len(prev) > 4 else ("", None)
        if temp_umo and temp_pair:
            self.temp.setdefault(temp_umo, {})[uid] = [
                temp_pair[0], temp_pair[1], int(time.time())]
        else:
            self.temp.get(self.umo_of(event), {}).pop(uid, None)

        if not name:
            ok = await self.erase_relation(bot_id, uid)
            return "已撤销：关系恢复成没设置的样子。\n下一条消息起按默认的说话方式。" \
                + self.saved_note(ok)
        self.data.setdefault(bot_id, {})[uid] = name
        entry = self.meta_of(bot_id, uid, create=True)
        if note:
            entry["note"] = note
        else:
            entry.pop("note", None)
        entry["at"] = int(time.time())
        ok = await self.save_data()
        out = [f"已撤销：关系回到「{self.format_relation(name)}」。"]
        if note:
            out.append(f"备注也一并恢复：{note}")
        if temp_pair:
            out.append(f"这个会话的临时关系也恢复为「{temp_pair[0]}」")
        return "\n".join(out) + self.saved_note(ok)

    async def do_clear(self, event, target_uid: Optional[str] = None) -> str:
        bot_id = self.get_bot_id(event)
        uid = str(event.get_sender_id())
        qq = target_uid or uid
        if target_uid and not self.is_admin(event):
            return "只有管理员可以帮别人清除关系。"
        if not self.session_allowed(event):
            return SESSION_DENIED_TEXT
        had = qq in self.data.get(bot_id, {})
        temp_had = self.temp.get(self.umo_of(event), {}).get(qq) is not None
        if not had and not temp_had:
            # 旧版在确认有没有东西可清之前就拍快照。后果：本来没设过关系的人
            # 多发一次「清除关系」，就把十分钟内那个还有效的快照覆盖成空态，
            # 于是再也撤不回上一次真正的改动。只在真的有东西时才拍。
            return "还没给你设置过关系（撤销快照没动）。\n可选：关系列表"
        if not target_uid:
            self.remember(event, bot_id, qq)
        self.temp.get(self.umo_of(event), {}).pop(qq, None)
        ok = await self.erase_relation(bot_id, qq)
        back = (
            f"接下来按配置里的默认关系（{self.default_relation()}）对待。"
            if self.config.get("enable_default_relation", False)
            else "下一条消息起恢复默认的说话方式。"
        )
        bits = []
        if had:
            bits.append(f"已清除用户 {qq} 的关系，" if target_uid else "关系已清除，")
        if temp_had:
            bits.append("这个会话的临时关系也关了，")
        return "".join(bits) + back + "\n要回去发：撤销关系" + self.saved_note(ok)

    async def do_note(self, event, text: str, target_uid: Optional[str] = None) -> str:
        bot_id = self.get_bot_id(event)
        sender = str(event.get_sender_id())
        qq = target_uid or sender
        if target_uid and not self.is_admin(event):
            return "只有管理员可以帮别人加备注。"
        if not self.session_allowed(event):
            return SESSION_DENIED_TEXT
        raw = re.sub(r"^@\S+\s*", "", clean_relation_name(text))
        clearing = raw in ("清空", "清除", "删掉", "去掉", "不要了")
        if not raw:
            return (
                "用法：关系备注 <一句话>\n"
                "最常用的是告诉他该怎么称呼你：关系备注 灰凝\n"
                "也能写别的：关系备注 她讨厌被叫全名\n"
                f"清掉备注：关系备注 清空　（最多 {self.max_note_len()} 字）"
            )
        umo = self.umo_of(event)
        # 临时关系在场时备注跟着临时的那条走，否则会出现「临时关系是恋人」配
        # 一条属于别的关系的备注。
        if not target_uid and qq in self.temp.get(umo, {}):
            self.remember(event, bot_id, qq)
            if clearing:
                self.temp[umo][qq][1] = ""
            else:
                self.temp[umo][qq][1] = self._clean_note(raw)
                self.temp[umo][qq][2] = int(time.time())
            return ("已清掉这个会话的备注。" if clearing
                    else f"已更新这个会话的备注：{self.temp[umo][qq][1]}")

        if not clearing and qq not in self.data.get(bot_id, {}):
            return "还没给你设置过关系，先发「设置关系 恋人」再来加备注。"
        if not target_uid:
            self.remember(event, bot_id, qq)
        if clearing:
            if qq in self.data.get(bot_id, {}):
                ok = await self.write_relation(bot_id, qq, self.data[bot_id][qq],
                                               "user" if qq == sender else "admin", note="")
                return "已清掉备注，关系还在。" + self.saved_note(ok)
            return "本来就没有备注。"
        ok = await self.write_relation(bot_id, qq, self.data[bot_id][qq],
                                       "user" if qq == sender else "admin", note=raw)
        prefix = f"已更新用户 {qq} 的备注：" if qq != sender else "已更新关系备注："
        return f"{prefix}{self._clean_note(raw)}" + self.saved_note(ok)

    async def do_mine(self, event) -> str:
        if not self.session_allowed(event):
            return SESSION_DENIED_TEXT
        name, note, label = self.effective(event)
        if name:
            out = f"你的关系：{self.format_relation(name)}（{label}）"
            if note:
                out += f"\n备注：{note}"
            if label == "临时":
                out += "\n只在这个会话有效。转成永久的：设置关系 " + name
            return out
        if self.config.get("enable_default_relation", False):
            return (f"你的关系：{self.default_relation()}（默认）\n"
                    "想换成别的：设置关系 恋人")
        return "未设置关系\n可选：关系列表"

    # ==============================
    # 指令
    # ==============================
    @filter.command("设置关系", alias={"关系设定"})
    async def set_relation(self, event: AstrMessageEvent, relation: GreedyStr = ""):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        yield event.plain_result(
            await self.do_set(event, str(relation), targets=self.at_targets(event))
        )

    @filter.command("临时关系")
    async def temp_relation(self, event: AstrMessageEvent, relation: GreedyStr = ""):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        yield event.plain_result(await self.do_temp(event, str(relation)))

    @filter.command("撤销关系")
    async def undo_relation(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        yield event.plain_result(await self.do_undo(event))

    @filter.command("清除关系", alias={"删除关系"})
    async def clear_relation(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        yield event.plain_result(await self.do_clear(event, self.at_target(event)))

    @filter.command("关系备注")
    async def relation_note(self, event: AstrMessageEvent, text: GreedyStr = ""):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        yield event.plain_result(await self.do_note(event, str(text), self.at_target(event)))

    @filter.command("查看我的关系", alias={"我的关系"})
    async def my_relation(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        yield event.plain_result(await self.do_mine(event))

    @filter.command("关系列表", alias={"可用关系"})
    async def relation_list(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        # 传自己的备注进去：这里展示的是「模型实际会收到什么」，
        # 不带备注的话用户拿它调提示词，看到的跟真实注入不是一回事。
        note = self.effective(event)[1] if self.relation_for(event) else ""
        yield event.plain_result(self.list_text(str(arg), note))

    @filter.command("关系详情")
    async def relation_detail(self, event: AstrMessageEvent, name: GreedyStr = ""):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        name = clean_relation_name(str(name))
        if not name:
            yield event.plain_result(
                "用法：关系详情 恋人\n会显示这条关系的完整说明，"
                "以及模型实际收到的内容。")
            return
        uid = str(event.get_sender_id())
        note = self.effective(event)[1] if self.relation_for(event) == name else ""
        yield event.plain_result(self.detail_text(name, note))

    @filter.command("查看所有关系")
    async def all_relation(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        if not self.is_admin(event):
            yield event.plain_result("权限不足")
            return
        yield event.plain_result(await self.admin_text(str(arg), mode="all"))

    @filter.command("关系统计")
    async def relation_stat(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        if not self.is_admin(event):
            yield event.plain_result("权限不足")
            return
        yield event.plain_result(await self.admin_text("", mode="stat"))

    @filter.command("关系管理")
    async def relation_admin(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        if not self.is_admin(event):
            yield event.plain_result("权限不足")
            return
        yield event.plain_result(await self.admin_text(str(arg)))

    @filter.command("关系帮助")
    async def relation_help(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        yield event.plain_result(HELP_TEXT)

    # ==============================
    # 裸指令入口
    # ==============================
    @filter.custom_filter(BareCommandFilter)
    async def bare_command(self, event: AstrMessageEvent):
        """群聊里不带唤醒前缀直接打「设置关系 恋人」也能用。"""
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        parsed = parse_command(event.get_message_str())
        if not parsed:
            return
        action, arg = parsed
        if action == "set":
            text = await self.do_set(event, arg, targets=self.at_targets(event))
        elif action == "temp":
            text = await self.do_temp(event, arg)
        elif action == "undo":
            text = await self.do_undo(event)
        elif action == "clear":
            text = await self.do_clear(event, self.at_target(event))
        elif action == "note":
            text = await self.do_note(event, arg, self.at_target(event))
        elif action == "mine":
            text = await self.do_mine(event)
        elif action == "list":
            note = self.effective(event)[1] if self.relation_for(event) else ""
            text = self.list_text(arg, note)
        elif action == "detail":
            name = clean_relation_name(arg)
            uid = str(event.get_sender_id())
            note = self.effective(event)[1] if self.relation_for(event) == name else ""
            text = (self.detail_text(name, note) if name else
                    "用法：关系详情 恋人")
        elif action == "admin":
            if not self.is_admin(event):
                text = "权限不足"
            else:
                text = await self.admin_text(arg)
        elif action == "all":
            if not self.is_admin(event):
                text = "权限不足"
            else:
                text = await self.admin_text(arg, mode="all")
        elif action == "stat":
            if not self.is_admin(event):
                text = "权限不足"
            else:
                text = await self.admin_text("", mode="stat")
        else:
            text = HELP_TEXT
        yield event.plain_result(text)

    # ==============================
    # 关系库文本
    # ==============================
    def list_text(self, arg: str, note: str = "") -> str:
        arg = clean_relation_name(arg)
        if not arg:
            return self.overview_text()
        if arg in RELATIONS:
            return self.detail_text(arg, note)
        if arg in DEPRECATED:
            return self.detail_text(arg, note)

        for category, blurb in CATEGORY_BLURBS:
            if category.startswith(arg):
                return self.category_text(category, blurb)

        # 搜索路径必须关掉 allow_custom，否则搜到的东西会被当成自定义关系名。
        name, _alias, candidates = resolve_relation_input(
            arg, self.fuzzy_enabled(), allow_custom=False)
        if name:
            return self.detail_text(name, note)
        if candidates:
            lines = [f"与「{arg}」相关的（{len(candidates)} 条）："]
            for category, names in group_by_category(candidates):
                lines.append(f"　{category}：{'、'.join(names)}")
            lines.append(f"\n看完整说明与模型实际收到的内容：关系列表 {candidates[0]}")
            return "\n".join(lines)
        return self.not_found_text(arg)

    def not_found_text(self, arg: str) -> str:
        return (
            f"没找到与「{arg}」相关的关系。\n"
            "换个词试试（也可以只打一个字：「关系列表 病」），"
            "或者分类直接发：关系列表 " + " ｜ 关系列表 ".join(
                c for c, _ in CATEGORY_BLURBS)
        )

    def overview_text(self) -> str:
        rp_total = sum(1 for _, (_, rp, _) in RELATIONS.items() if rp)
        lines = [
            f"【关系识别】预设关系 {len(RELATIONS)} 种（其中 {rp_total} 条为角色扮演向），"
            f"共 {len(CATEGORY_BLURBS)} 类",
            "",
        ]
        # 先给一份能直接用的短名单。139 条分类罗列看着全，但没人会从头挑到尾——
        # 第一次用的人看到九个分类只会更不知道选哪个。
        hot = [n for n in POPULAR if n in RELATIONS]
        lines.append("「先看这些」——八成的人设关系都在这里面：")
        lines.append("　" + "、".join(
            n + ("【演绎】" if is_roleplay(n) else "") for n in hot))
        lines.append("")
        lines.append("全部九个分类：")
        for category, blurb in CATEGORY_BLURBS:
            names = names_of_category(category)
            if not names:
                continue
            rp_count = sum(1 for n in names if RELATIONS[n][1])
            if rp_count == len(names):
                mark = "【全是演绎向】"
            elif rp_count:
                mark = f"【{rp_count} 条演绎向】"
            else:
                mark = ""
            lines.append(f"◇ {category} {len(names)} 条{mark}　{blurb}")
        lines.append("")
        lines.append("用法：设置关系 恋人 ｜ 关系列表 恋爱 ｜ 关系列表 恋人")
        lines.append("标「演绎向」的是角色扮演关系，AI 会按设定身份来演绎；不带的就是正常相处。")
        lines.append("打不全也没关系：「设置关系 女朋友」「设置关系 病」都能认。")
        lines.append("只想在某个群临时玩一下：临时关系 恋人（不落盘、不影响你在别处的设定）")
        return "\n".join(lines)

    def category_text(self, category: str, blurb: str) -> str:
        names = names_of_category(category)
        lines = [f"◇ {category}（{len(names)} 条）", f"　{blurb}"]
        lines += self.lines_of(names)
        lines.append("")
        # 给一个本分类里的真名字当例子：写「设置关系 名字」会被用户原样发出来，
        # 于是真多出一个名叫「名字」的关系。
        example = names[0] if names else "朋友"
        lines.append(f"设一个：设置关系 {example}（也可以写你自己想要的任何名字）")
        return "\n".join(lines)

    def lines_of(self, names: List[str]) -> List[str]:
        return [
            f"· {name}{'【演绎】' if is_roleplay(name) else ''}｜{relation_desc(name)}"
            for name in names
        ]

    def detail_text(self, name: str, note: str = "") -> str:
        desc = relation_desc(name)
        if not desc:
            return (
                f"「{name}」已经不在预设里了（{relation_category(name)}），"
                "但你之前设的关系仍然有效，会按字面含义相处。\n"
                f"想换成别的：设置关系 {name}"
            )
        category, roleplay, _ = RELATIONS[name]
        lines = [
            f"{name}｜{category}｜{'角色扮演向' if roleplay else '正常相处'}",
            desc,
            "",
            "模型实际会收到这样一段（每条消息都发一次，不落历史）：",
            build_hint(name, note),
        ]
        if not note:
            lines.append("\n（你那还没写备注。告诉他该怎么称呼你：关系备注 灰凝）")
        return "\n".join(lines)

    def collect_rows(self, bot: str = "", q: str = "") -> List[Dict[str, Any]]:
        """所有已保存的关系，按最近改动排序。关系管理/查看所有关系走这里。"""
        rows = []
        needle = q.strip().lower()
        for bot_id, users in self.data.items():
            if bot and bot_id != bot:
                continue
            for uid, name in users.items():
                if needle and needle not in (uid + " " + name).lower():
                    continue
                meta = self.meta_of(bot_id, uid)
                rows.append({
                    "bot": bot_id,
                    "uid": uid,
                    "name": name,
                    "note": meta.get("note", ""),
                    "source": meta.get("src", "user"),
                    "source_label": self.SOURCE_LABELS.get(
                        meta.get("src", "user"), "自己设置"),
                    "category": relation_category(name),
                    "preset": is_preset(name),
                    "roleplay": is_roleplay(name),
                    "at": meta.get("at", 0),
                })
        rows.sort(key=lambda r: (-int(r["at"] or 0), r["bot"], r["uid"]))
        return rows

    def bots(self) -> List[str]:
        return sorted(self.data.keys())

    def total_users(self) -> int:
        return sum(len(users) for users in self.data.values())

    PAGE_SIZE = 15

    async def admin_text(self, arg: str = "", mode: str = "admin") -> str:
        """管理员指令的正文。mode：admin=总览 / all=全部 / stat=统计。

        分页参数写进入口：关系管理 10001 2 就是「只看含 10001 的，第 2 页」。
        """
        arg = clean_relation_name(arg)
        page = 1
        words = arg.split()
        if words and words[-1].isdigit() and len(words) > 1:
            page = max(1, int(words[-1]))
            arg = " ".join(words[:-1])
        bot = ""
        for word in words:
            if word in self.bots():
                bot = word
                arg = " ".join(w for w in words if w != word)
                break

        total = self.total_users()
        if not total:
            return ("现在还没有任何关系。\n"
                    "用户发「设置关系 恋人」就会出现在这里。")

        rows = self.collect_rows(bot=bot, q=arg)

        if mode == "stat":
            by_cat = Counter(r["category"] for r in self.collect_rows())
            by_source = Counter(r["source_label"] for r in self.collect_rows())
            lines = [f"====== 关系统计（共 {total} 人）======"]
            lines.append("按机器人：" + "、".join(
                f"{b} {len(u)}" for b, u in sorted(self.data.items())))
            lines.append("按分类：" + "、".join(
                f"{c} {n}" for c, n in by_cat.most_common()))
            lines.append("按来源：" + "、".join(
                f"{s} {n}" for s, n in by_source.most_common()))
            top = Counter(r["name"] for r in self.collect_rows()).most_common(8)
            lines.append("最常用：" + "、".join(f"{n}×{c}" for n, c in top))
            return "\n".join(lines)

        if not rows:
            return (f"没有匹配「{arg}」的关系（共 {total} 人）。\n"
                    "换个关键词，或「关系管理」不带参数看全部。")

        pages = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = min(page, pages)
        start = (page - 1) * self.PAGE_SIZE
        head = f"====== 关系总览（共 {total} 人 / 命中 {len(rows)} 条 / 第 {page}/{pages} 页）======"
        lines = [head]
        for row in rows[start:start + self.PAGE_SIZE]:
            extra = f"　备注：{row['note']}" if row["note"] else ""
            lines.append(f"{row['bot']} / {row['uid']}：{row['name']}"
                         f"（{row['source_label']}）{extra}")
        lines.append("")
        nxt = f"关系管理 {arg} {page + 1}".replace("  ", " ").strip()
        lines.append(f"翻页：{nxt}" if page < pages else "已经是最后一页。")
        lines.append("改单个：设置关系 @QQ 关系名 ｜ 清除关系 @QQ")
        lines.append("搜索：关系管理 QQ号或关系名 ｜ 关系统计")
        return "\n".join(lines)

    # ==============================
    # LLM 工具：让模型自己维护关系
    # ==============================
    SOURCE_LABELS = {"user": "自己设置", "admin": "管理员设置", "model": "模型代设"}

    def _pending_of(self, bot_id: str, uid: str) -> Optional[List[Any]]:
        item = self.pending.get(bot_id, {}).get(uid)
        if not item:
            return None
        if int(time.time()) - int(item[2]) > PENDING_TTL:
            self.pending[bot_id].pop(uid, None)
            return None
        return item

    @filter.llm_tool(name="set_relation")
    async def tool_set_relation(
        self, event: AstrMessageEvent, relation: str, note: str = "",
        confirm: bool = False,
    ) -> str:
        '''记录这位用户与你的关系，或修改已有的关系。

        只在用户明确要求改变关系时才调用——他说过「我们是什么关系」「以后你当我女朋友」
        「忘掉刚才那层关系」这类话，或者明确认可了你刚提的关系。普通闲聊绝不要调用，
        更不要自己替用户认定关系。

        用户还没明确表态时：先用嘴问一句「要不要我把关系记成 X？」，得到肯定之后再调用一次，
        并把 confirm 设为 true。第一次调用（confirm 为 false）只会生成一个待确认的建议，
        不会真的保存——所以你必须问过才落库。

        Args:
            relation(string): 关系名。预设名（恋人、朋友、魅魔）、口语说法（女朋友、老公）或任意自定义名都可以。
            note(string): 最常用是告诉 AI 该怎么称呼这位用户，例如「灰凝」。也可以写别的关系细节。没有就留空。
            confirm(boolean): 用户已经明确点头时才传 true。
        '''
        if not self.model_tool_enabled():
            return "本会话未开放关系设定工具，请让用户直接发「设置关系 关系名」。"
        if not self.session_allowed(event):
            return DENIED_TOOL
        bot_id = self.get_bot_id(event)
        uid = str(event.get_sender_id())
        name, alias, candidates = resolve_relation_input(relation, self.fuzzy_enabled())
        if not name and candidates:
            groups = "；".join(
                f"{cat}：" + "、".join(ns) for cat, ns in group_by_category(candidates))
            return f"「{relation}」匹配到多个预设：{groups}。请让用户挑一个。"
        if not name:
            return f"没有「{relation}」这条关系。预设名可以从 list_relations 工具拿，或直接用自定义名。"
        if len(name) > MAX_RELATION_LEN:
            return f"「{relation}」太长了（{len(name)} 字），请换个更短的说法。"

        pending = self._pending_of(bot_id, uid)
        if self.require_confirm() and not (pending and confirm):
            self.pending.setdefault(bot_id, {})[uid] = [
                name, self._clean_note(note), int(time.time()),
            ]
            await self.save_data()
            alias_note = f"（用户说的是「{alias}」，按「{name}」记）" if alias else ""
            return (
                f"待确认，尚未保存：{alias_note}要把关系记成「{name}」吗？"
                "请先口头问用户，得到肯定后再带 confirm=true 调一次。"
            )

        await self.write_relation(bot_id, uid, name, "model",
                                  note=note or (pending[1] if pending else ""))
        return f"已保存：这位用户与你的关系现在是「{name}」。用平常的样子相处就行，不用提起这件事。"

    @filter.llm_tool(name="get_relation")
    async def tool_get_relation(self, event: AstrMessageEvent) -> str:
        '''查询这位用户当前与你的关系设置。

        用户问「我们是什么关系」「你现在怎么称呼我」之类时调用。

        Args:
        '''
        if not self.session_allowed(event):
            return DENIED_TOOL
        name, note, label = self.effective(event)
        if not name:
            if self.config.get("enable_default_relation", False):
                return f"没有单独设定过关系，目前按默认关系「{self.default_relation()}」相处。"
            return "没有设定过关系，目前按你本来的样子相处。"
        extra = "；只在这个会话有效" if label == "临时" else ""
        return f"当前关系：{name}（{label}{extra}）" + (f"；备注：{note}" if note else "")

    @filter.llm_tool(name="clear_relation")
    async def tool_clear_relation(
        self, event: AstrMessageEvent, force: bool = False,
    ) -> str:
        '''解除或修改这位用户与你的关系。

        只在用户明确要求「忘掉这段关系」「不用那个设定了」「我们重新开始」时调用。
        如果用户只是要换成别的关系，用 set_relation 改，不要先清除。

        Args:
            force(boolean): 当前会话里有临时关系时，用户已经明确说了要永久清掉才传 true。
        '''
        if not self.session_allowed(event):
            return DENIED_TOOL
        bot_id = self.get_bot_id(event)
        uid = str(event.get_sender_id())
        # 临时关系在场时，模型看到的关系其实是临时那条；清它之前得先说清楚
        # 「清掉临时关系」和「永久清除」不是一回事。
        temp = self.temp_of(event, uid)
        if temp and not force:
            return ("这个会话里现在用的是临时关系（只在本对话有效）。"
                    "要转成永久的请用 set_relation 重新设一次并让用户确认。")
        if uid not in self.data.get(bot_id, {}) and not temp:
            return "本来就没有设定过关系，无需解除。"
        # force=True 意味着用户说了「永久清掉」。这时必须把临时层也拆了——
        # 只删存档却回一句「已解除」，而实际生效的临时关系还在，是最气人的一种错。
        self.temp.get(self.umo_of(event), {}).pop(uid, None)
        ok = await self.erase_relation(bot_id, uid)
        return ("已解除关系设定（包括这个会话的临时关系）。"
                "请用平常的样子和他说话，不要提起这件事。" + self.saved_note(ok))

    @filter.llm_tool(name="list_relations")
    async def tool_list_relations(
        self, event: AstrMessageEvent, keyword: str = "",
    ) -> str:
        '''列出可用的关系预设。

        用户想看有哪些关系可选、或者报了个名字让你确认是不是这个时调用。
        不想每次都查也可以不调——直接照他说的字面意思相处通常就够。

        Args:
            keyword(string): 分类名或关键词，留空则只给分类总览。
        '''
        if not self.session_allowed(event):
            return DENIED_TOOL
        if keyword:
            return self.list_text(keyword)
        cats = []
        for category, blurb in CATEGORY_BLURBS:
            cats.append(f"{category}（{len(names_of_category(category))} 条）：{blurb}")
        return (
            "可选关系分类：\n" + "\n".join(cats)
            + "\n\n只设过一次关系的话，最常用的是：朋友、知己、恋人、夫妻、家人、"
            "老师、老板、搭档、树洞、军师。也可以直接用用户自己的话当关系名。"
        )

    # ==============================
    # LLM 上下文注入
    # ==============================
    @filter.on_llm_request()
    async def inject_relation(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if not self.session_allowed(event):
                return
            bot_id = self.get_bot_id(event)
            uid = str(event.get_sender_id())
            relation, note, _label = self.effective(event)
            if not relation:
                return
            # 每条消息都发一次。走 mark_as_temp 时这段不落历史，靠的是「模型这一轮看得到」，
            # 而不是「模型记着上一轮」，所以历史被截断、换会话都不会让关系失效。
            text = build_hint(relation, note)
            req.extra_user_content_parts.append(temp_text_part(text))
            logger.debug(
                "[关系插件] 注入关系提示 机器人:%s uid:%s 关系:%s（%d 字）",
                bot_id, uid, relation, len(text),
            )
        except Exception:
            logger.error("[关系插件] LLM注入异常")
            logger.error(traceback.format_exc())
