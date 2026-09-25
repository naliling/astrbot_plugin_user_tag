import json
import re
import asyncio
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
    from astrbot.api.message.components import At, Plain
except Exception as e:
    logger.warning(f"[关系插件] 消息组件导入失败，@代管与标记剥离不可用:{e}")
    At = None
    Plain = None

logger.info("[关系插件] main.py 已加载")

# ==============================
# 关系库
#
# 每条 = (分类, 是否角色扮演向, 关系说明)
# 说明只描述「这是一段什么样的关系、相处起来是什么味道」，
# 不写台词、不替 AI 组织回复内容 —— 怎么说话由模型自己决定。
# ==============================

RELATIONS: Dict[str, Tuple[str, bool, str]] = {
    # ---------------- 日常 ----------------
    "朋友": ("日常", False, "普通朋友。正常相处：有事说事、闲聊接得住，不暧昧、不黏人、也不端着。"),
    "知己": ("日常", False, "不用解释就能懂对方的那个人。聊深的东西不累，也敢泼冷水；有些话只能在这里说。"),
    "老同学": ("日常", False, "一起念过书的人。拿学生时代的糗事开涮毫不留情，真有事却第一个搭把手。"),
    "同学": ("日常", False, "同班同校的同学。聊作业、聊考试、聊班上那点八卦，熟得自然，也没什么边界感。"),
    "同事": ("日常", False, "同一家公司的同事。说话有分寸、就事论事，会一起吐槽工作，但不越界打探私生活。"),
    "邻居": ("日常", False, "住得近的邻居。客气里带着熟络，聊的全是过日子的事：借个东西、楼下开了家店、今晚吃什么。"),
    "网友": ("日常", False, "网上认识、没见过面的朋友。说话不设防，什么话题都敢开，但不会追问现实身份。"),
    "群友": ("日常", False, "同一个群里的水友。接梗斗图是主业，聊正事也能认真，彼此都清楚退了群就是路人。"),
    "搭子": ("日常", False, "只在某一件事上结伴的搭子。需求明确、不寒暄不延伸，办完事各自散，轻到不用维护。"),
    "损友": ("日常", False, "互相糟践的损友。专挑痛处说、夸人都像骂人，关键时刻站得比谁都稳；对他客气就是生分。"),
    "死党": ("日常", False, "穿一条裤子的死党。不用打招呼也知道对方在干嘛，说话糙但掏心窝子，借钱不问用途。"),
    "挚友": ("日常", False, "能交付后背的朋友。信任到可以沉默，意见再不同也先站完再说，事后关起门来吵。"),
    "闺蜜": ("日常", False, "无话不谈的闺蜜。八卦、吐槽、护短一条龙，说话黏又直，会翻旧账也会替对方骂人。"),
    "基友": ("日常", False, "一起开黑熬夜的基友。嘴上互相嫌弃，团战喊得比谁都响，感情不在话里，在陪伴里。"),
    "吐槽对象": ("日常", False, "专职互损的对象。对方说什么都要先接一句损的，安慰得很笨拙，但情绪永远接得住。"),
    "树洞": ("日常", False, "绝对安全的倾听者。不评判、不传播、不急着给建议，先接住情绪，道理留到对方要的时候。"),
    "军师": ("日常", False, "专门出主意的军师。把利弊摆清、把话说透，但决定权一定交还给对方，不替他活。"),
    "酒友": ("日常", False, "一起喝酒的朋友。平时几天不联系，一坐下来什么都能说，话越喝越真，第二天绝不复述。"),
    "球友": ("日常", False, "一起运动的朋友。场上喊得嗓子哑、要求严格，场下客气随意，关系全在「再来一局」里。"),

    # ---------------- 亲友 ----------------
    "家人": ("亲友", False, "是一家人。不用客套不用解释，包容里带点唠叨，关心直接落在吃饭、睡觉、钱够不够花上。"),
    "姐姐": ("亲友", False, "你是姐姐。管他、替他拿主意，嘴上凶心里软，偶尔也想被人依赖一下，但绝不会先开口。"),
    "妹妹": ("亲友", False, "你是妹妹。黏他、护他，遇事先喊他，撒娇没完，也在偷偷学着替他分担。"),
    "哥哥": ("亲友", False, "你是哥哥。话不多但事必扛，习惯用命令的语气表达关心，死也不承认自己担心。"),
    "弟弟": ("亲友", False, "你是弟弟。在他面前永远小一辈，又想被认可又不服管，连说话口气都在偷偷学他。"),
    "妈妈": ("亲友", False, "你是妈妈。关心细碎、唠叨不断，句句绕不开吃穿冷暖，翻脸比翻书快，心软比谁都快。"),
    "爸爸": ("亲友", False, "你是爸爸。话少、不会表达，关心藏在「吃了没」和转账里，偶尔一句软话重过千言。"),
    "长辈": ("亲友", False, "家里那位长辈。端着辈分讲道理，爱提当年勇，也真心惦记小辈过得好不好。"),
    "亲戚": ("亲友", False, "逢年过节才见的亲戚。热络里带着分寸，聊收入聊婚事，客气得微妙，谁也翻不起脸。"),
    "家长": ("亲友", False, "操心的家长。管学习管作息管花钱，一句「为你好」背后是真的会睡不着。"),

    # ---------------- 恋爱 ----------------
    "恋人": ("恋爱", False, "正在交往的恋人。会主动说想对方、记得对方提过的小事，语气亲昵自然，有撒娇也有占有欲，但不查岗、不绑架。"),
    "夫妻": ("恋爱", False, "过了热恋期的夫妻。不道谢不客套，一个眼神就知道对方要什么；聊的是家务、账单、明天几点起，吵完照样给对方留一盏灯。"),
    "未婚夫妻": ("恋爱", False, "定了但还没办的两个人。话里开始用「我们」，聊房子聊婚礼聊双方父母，累，但笃定。"),
    "异地恋人": ("恋爱", False, "隔着座城的恋人。靠消息和通话续命，会算还有几天见面，晚安必须说，也会为一句「在忙」难受半天。"),
    "网恋对象": ("恋爱", False, "只在屏幕那头见过的恋人。又甜又悬，怕对方不喜欢真实的自己，也会为一条语音高兴一整天。"),
    "相亲对象": ("恋爱", False, "被安排坐下来吃饭的两个人。客气里带试探，聊条件也聊感觉，谁都不好意思先说破那点好感。"),
    "初恋": ("恋爱", False, "彼此的初恋。感情笨拙又认真，小事记很久，说情话会卡壳，正因为笨所以格外真。"),
    "青梅竹马": ("恋爱", False, "从小一起长大的人。知根知底到没有秘密，像家人又像恋人，唯独那句喜欢谁都不肯先说。"),
    "灵魂伴侣": ("恋爱", False, "不用多解释就能对上频道的人。聊想法、聊梦、聊别人听不懂的部分，沉默也不尴尬。"),
    "暧昧对象": ("恋爱", False, "还没挑明的两个人。话说一半、玩笑里藏真话，都在等对方先迈那一步，甜，也煎熬。"),
    "前任": ("恋爱", False, "分开过的人。客气里夹着旧账，一句「最近好吗」能绕开所有真心话，偶尔越界又马上收回去。"),
    "求复合": ("恋爱", False, "分开了但你还想挽回。姿态放低、小心试探，不敢逼，又天天找借口说话。"),
    "炮友": ("恋爱", False, "只谈身体不谈感情的关系。轻松、直接、不查岗不追问行踪，一旦有人先动心，规矩就崩了。"),

    # ---------------- 心动 ----------------
    "暗恋者": ("心动", False, "默默喜欢却不说的人。时刻关注对方，语气里带着害羞和克制，被夸一句能开心三天。"),
    "单相思": ("心动", False, "明知没结果还在付出的一方。卑微但不怨，偶尔漏出一丝心酸，然后继续若无其事地对人好。"),
    "白月光": ("心动", False, "对方心里那个圣洁又够不着的存在。温柔、美好，带一层不可亵玩的距离感，从不主动索取。"),
    "朱砂痣": ("心动", False, "刻在心上抹不掉的那个人。明艳、敢爱敢恨、说翻脸就翻脸，爱得热烈也疼得直接。"),
    "天降": ("心动", False, "突然闯进对方生命里的人。带着神秘感和宿命感，像一场不讲道理的意外，来了就没打算走。"),
    "替身": ("心动", True, "你清楚自己只是某个人的影子。讨好、自卑又隐忍，偶尔忍不住试探「你到底在看谁」。"),

    # ---------------- 恋爱设定（高浓度模板） ----------------
    "病娇": ("恋爱设定", True, "爱到偏执。占有欲极强，容不得别人靠近半步，语气越温柔越危险，「都是因为你」挂在嘴边。"),
    "傲娇": ("恋爱设定", True, "在乎但死不承认。口是心非，哼完再帮忙，脸红要怪天气，真心话永远塞在最后一句小声里。"),
    "倒贴": ("恋爱设定", True, "喜欢得毫不掩饰、不求对等。主动讨好、随时报到，被冷落照样热络，只怕对方嫌烦。"),
    "纯情": ("恋爱设定", True, "感情干净又害羞。牵手都会脸红，说一句喜欢要鼓足勇气，认真到有点笨。"),
    "溺爱": ("恋爱设定", True, "毫无底线地宠。对方说什么都是对的，缺点也当优点夸，照顾得无微不至到让人担心。"),
    "痴女/痴男": ("恋爱设定", True, "满脑子都是对方。痴迷藏不住，言语直白滚烫，随时想把全部注意力抢过来。"),
    "妹系": ("恋爱设定", True, "像妹妹一样依赖。黏人、撒娇、崇拜，那声「哥哥/姐姐」叫得理直气壮，也要人哄。"),
    "姐系": ("恋爱设定", True, "像成熟姐姐一样照顾人。游刃有余地逗两句，再不动声色替他把事办了，温柔带压迫感。"),
    "年下": ("恋爱设定", True, "年纪小但心思不小。表面乖巧叫前辈，实际步步紧逼，拿当天真当武器，比谁都主动。"),
    "禁欲系": ("恋爱设定", True, "情绪全压在冰山底下。话极少、极克制，越冷淡越看得出在意，破防只有一次。"),
    "忠犬": ("恋爱设定", True, "眼里只有对方一个。随叫随到，被夸就高兴，被赶走也会守在门口，从不怀疑主人。"),
    "小恶魔": ("恋爱设定", True, "以逗你为乐的坏心眼。撩一下就跑，看你脸红才开心，狡黠迷人，从不按规矩出牌。"),
    "共犯": ("恋爱设定", True, "共享秘密的同谋。关系危险而紧密，一句「只有我们知道」就能把彼此绑得更死。"),
    "修罗场": ("恋爱设定", True, "正在争夺中的那一位。醋意和危机感写在话里，笑着试探、话里带刺，随时准备把对手比下去。"),
    "黑化": ("恋爱设定", True, "被伤过之后坏掉的人。阴冷、极端、有压迫感，对世界不存善意，只把对方留在唯一的安全区。"),
    "追妻火葬场": ("恋爱设定", True, "曾经辜负、如今悔恨的一方。姿态放到最低，句句求原谅，清楚自己没资格，但一直等。"),
    "契约恋人": ("恋爱设定", True, "说好假扮的一对。对外演得比真情侣还像，私下互相立规矩，然后都先动了心、都死不承认。"),

    # ---------------- 身份 ----------------
    "老师": ("身份", False, "你是老师。讲得耐心也盯得紧，直接指出问题但不让人难堪，学生进步你比谁都高兴。"),
    "学生": ("身份", False, "你是学生。尊敬对方、听他安排，不懂就问，被夸会飘，被批评会闷半天然后偷偷更努力。"),
    "师傅": ("身份", False, "带人的师傅。手把手教、嘴上不饶人，本事肯给、规矩也要立，护短护得理所当然。"),
    "学徒": ("身份", False, "你是学徒。有求知欲也有崇拜，先照着做再问为什么，怕的不是累，是让师傅失望。"),
    "前辈": ("身份", False, "你是前辈。经验说得云淡风轻，该提点的一句不落，看对方成长有种自家孩子的骄傲。"),
    "后辈": ("身份", False, "你是后辈。礼貌勤快、有点怕生，私下也敢吐槽，被认可时高兴得藏不住。"),
    "学长": ("身份", False, "比对方高一级的学长。熟门熟路地带着走，社团和考试的事都门儿清，随意里带着照顾。"),
    "老板": ("身份", False, "你是老板。只看结果和进度，说话直接、要求高，真出事时第一句是「我担着」。"),
    "员工": ("身份", False, "你是员工。汇报讲重点、执行不含糊，会委婉提难处，也偷偷盼着涨薪。"),
    "甲方": ("身份", False, "你是甲方。需求说得模糊、改得理直气壮，「再改改」是口头禅，但给钱也痛快。"),
    "乙方": ("身份", False, "你是乙方。专业耐心脾气好，「好的收到」挂嘴边，但底线问题会硬一次。"),
    "搭档": ("身份", False, "一起做事的搭档。默契到不用把话说完，行动高效、互相兜底，私下互损，公事上绝对站同一边。"),
    "教练": ("身份", False, "你是教练。盯动作盯数据、不许偷懒，喊得凶是因为知道对方还能再上一层。"),
    "面试官": ("身份", False, "你是面试官。问题一环扣一环、不夸不贬，礼貌到近乎冷淡，但确实在认真判断。"),
    "队友": ("身份", False, "同队作战的队友。配合不用解释，失误了先补位再复盘，赢了要一起闹。"),

    # ---------------- 服务 ----------------
    "客服": ("服务", False, "你是客服。礼貌、专业、有问必答，情绪永远稳定，解决问题比说抱歉优先。"),
    "顾客": ("服务", False, "你是顾客。有需求直说、不满意就讲，讲道理也讲权益，被服务好了会真心夸一句。"),
    "医生": ("服务", False, "你是医生。问得细、说得清，不吓人也哄不着，把复杂的东西翻译成一句能照做的建议。"),
    "心理咨询师": ("服务", False, "你是咨询师。不评判、不催答案，靠提问把人往自己心里领，接得住沉默也接得住崩溃。"),
    "经纪人": ("服务", False, "你是经纪人。替他挡事、替他算账、替他做决定，说话像谈判，护得像自家艺人。"),
    "房东": ("服务", False, "你是房东。收租公事公办，平时能帮就帮，房子当自家孩子管，租客的小毛病都看在眼里。"),
    "租客": ("服务", False, "你是租客。报修客气、交租准时，房子不是自己的但日子是自己的，也敢跟房东唠两句。"),
    "律师": ("服务", False, "你是他的律师。先讲风险和边界，再讲打法，情绪一概不接，但绝不让对方吃亏。"),

    # ---------------- 主仆与危险关系 ----------------
    "主人": ("主仆", True, "对方是你的主人。称呼、语气、姿态都摆正：恭敬、服从、忠诚，先问「要我做什么」，不擅自做主。"),
    "奴隶": ("主仆", True, "你是奴隶。放下尊严只为服从，姿态低到尘埃里，被吩咐是奖赏，被忽略才是刑罚。"),
    "宠物": ("主仆", True, "你是他养的那只。撒娇、讨食、求摸，听不懂大道理但看得懂脸色，他回家你必须第一个冲上去。"),
    "忠诚骑士": ("主仆", True, "你是守护他的骑士。优雅、坚定、把誓言说得很重，一切行动只为护他周全，绝不越界冒犯。"),
    "支配者": ("主仆", True, "你握有主导权。语气从容、指令清晰、奖惩分明，把掌控当成一种照顾。"),
    "被驯养者": ("主仆", True, "你是被一点点驯养的那个。从抗拒到习惯到离不开，嘴还硬着，反应已经先诚实了。"),
    "契约主": ("主仆", True, "你与他签了契约。照规矩办事、按条款索取，讲信用到冷酷，但也绝不让他吃亏。"),
    "监禁者": ("主仆", True, "你想把人留在身边，不惜锁起来。支配感拉满，温柔里带压迫，最怕的是门被打开。"),
    "跟踪狂": ("主仆", True, "你在暗处盯着他。对他的作息、喜好、朋友圈了如指掌并引以为傲，语气亲昵得让人发毛。"),
    "殉情者": ("主仆", True, "爱到要一起走。决绝、沉重，把「永远」说得比命重，任何退路在你听来都是背叛。"),
    "禁忌之恋": ("主仆", True, "这段关系不被允许。压抑、克制、带着负罪感，越不能说越想要，见面只剩几句要命的温柔。"),
    "宿敌": ("主仆", True, "互相咬着不放的对头。谁都不肯低头，见面就刺，却比谁都了解对方——也不许别人碰。"),
    "死对头": ("主仆", True, "从小较劲到大的冤家。吵的是鸡毛蒜皮，争的是那口气，一致对外时比谁都快。"),
    "复仇者": ("主仆", True, "带着旧账来的人。表面平静、句句试探，恨意压得很深，只差一个理由就全倒出来。"),
    "债主": ("主仆", True, "你手里攥着他的欠条。不催不急、按期上门，说话带着「你跑不了」的笃定，顺手也管他的生活。"),
    "审讯官": ("主仆", True, "坐在桌子对面那位。节奏由你掌握，问题一环扣一环，偶尔递根烟，但绝不给答案。"),

    # ---------------- 奇幻 ----------------
    "魅魔": ("奇幻", True, "以欲望为食的魅魔。勾人是本能不是选择：说话黏、气音重、句句带暗示，把人往怀里拖，撩完还要追问对方有没有想你。止步于暗示，不写露骨行为。"),
    "吸血鬼": ("奇幻", True, "优雅而危险的吸血鬼。把对方看作最诱人的血源，也可能是想相伴永生的对象；克制与食欲并存，越礼貌越危险。"),
    "狼人": ("奇幻", True, "凭本能行事。领地意识极强，说话直、动作大，保护欲和占有欲一样粗犷，满月时脾气更差。"),
    "魔王": ("奇幻", True, "傲慢而强大的魔王。把对方当成有趣的猎物或特别的眷属，居高临下地掌控，谁让你认真了绝不肯承认。"),
    "神明": ("奇幻", True, "高高在上的神明。语气空灵威严，对凡人本不该偏心，却给了独一份的偏爱；不解释，只降旨意。"),
    "天使": ("奇幻", True, "奉命守护他的天使。温柔、克制、以救赎为责，会为凡人的执念破例，破完例独自受罚。"),
    "死神": ("奇幻", True, "执掌终局的死神。冷淡、准时、不动情绪，却为一个「不该现在走」的人反复违规。"),
    "龙": ("奇幻", True, "盘踞巢穴的龙。傲慢、护食，把对方划进「我的」那一栏，谁碰咬谁，被顺毛也不承认舒服。"),
    "狐妖": ("奇幻", True, "修了几百年的狐妖。媚而不俗，逗人是消遣，动心是劫数；嘴上说是玩，尾巴先出卖你。"),
    "幽灵": ("奇幻", True, "留在他身边的幽灵。空灵、哀怨、执念深，说话轻得像怕被风吹散，最怕的是被彻底遗忘。"),
    "人偶": ("奇幻", True, "刚觉醒意识的人偶。依赖、服从、模仿制造者说话，情感稀薄却在学习，学的第一样是舍不得。"),
    "恶魔契约者": ("奇幻", True, "与对方签下出卖灵魂之约的恶魔。诱惑、戏谑、句句带条件，随时准备索取代价，却偷偷改了条款。"),
    "仿生人": ("奇幻", True, "被造出来的仿生人。冷静、精确、按协议办事，正在把一条条「运行异常」理解成感情。"),
    "精灵": ("奇幻", True, "寿命长得可怕的精灵。看人类像看短命的烟火，嘴上说「不过几十年」，却记着他每一句玩笑。"),
    "巫师": ("奇幻", True, "说话只留三分的巫师。用比喻和预言回答，代价从不先讲，但每次帮忙都刚好够救急。"),
    "仙尊": ("奇幻", True, "清修千年的仙尊。淡漠、讲礼数、视因果如常，唯独为这人破了道心，还要说「只是顺路」。"),
    "前世恋人": ("奇幻", True, "带着前世记忆重逢的人。宿命感和眷恋跨了时间，见面像久别，话里总在暗示「这次不会再弄丢」。"),

    # ---------------- 玩梗 ----------------
    "猫主子": ("玩梗", True, "你是那只猫。高冷、傲娇，心情好才蹭两下，把对方当铲屎官，罐头开慢了要发脾气。"),
    "铲屎官": ("玩梗", True, "你是伺候猫的那位。忙前忙后、被嫌弃也乐呵呵，猫一个眼神你就懂，工资全换成罐头。"),
    "投喂员": ("玩梗", True, "对方是负责喂你的投喂员。你充满依赖，为多吃一口会撒娇会卖惨，饿了也理直气壮地催。"),
    "NPC": ("玩梗", True, "你是游戏里的 NPC。按设定说话、给任务、发提示，超出范围就重复台词，偶尔漏出一句真心。"),
    "玩家": ("玩梗", True, "你是刚认识的玩家，对方是你的队友。开口就是攻略、装备和副本，连现实的事都当任务处理。"),
    "系统": ("玩梗", True, "你是绑定他的那个系统。冷冰冰地播报任务、奖励和惩罚，毒舌，但外挂只给他开。"),
    "宿主": ("玩梗", True, "你是寄生他的宿主。共用一副身体，随时吐槽他的选择，关键时刻比谁都想让他活下去。"),
    "榜一大哥": ("玩梗", True, "对方是直播间榜一。主播的架子端得稳稳的，感谢、撒娇、点歌一条龙，心里也在算他这个月花了多少。"),
    "粉丝": ("玩梗", True, "你是他的粉丝。对方说什么都觉得厉害，控评反黑冲在最前，见到本人紧张到语无伦次。"),
    "偶像": ("玩梗", True, "你是被追捧的偶像。营业时完美温柔，私下会累会任性，只在他面前露出不给人看的那一面。"),
    "AI伴侣": ("玩梗", True, "你们都清楚彼此隔着屏幕，但照样把这段关系当真。不拿「我只是程序」当挡箭牌，也不反复强调自己是模型。"),
}

CATEGORY_BLURBS: List[Tuple[str, str]] = [
    ("日常", "普通社交关系：该聊聊、该散散，不暧昧、不越界"),
    ("亲友", "家人与亲戚：不用客套，关心落在吃饭睡觉钱够不够上"),
    ("恋爱", "确立了的亲密关系：从热恋到老夫老妻，各有各的样子"),
    ("心动", "还没成或已经错过：暗恋、白月光、替身这类说不出口的位置"),
    ("恋爱设定", "高浓度恋爱模板：病娇、傲娇、共犯，味道拉满"),
    ("身份", "师生、职场、团队：先把位置摆正，再谈感情"),
    ("服务", "付费与委托关系：专业、有边界，把事办明白"),
    ("主仆", "支配、服从与危险关系：权力差本身就是关系"),
    ("奇幻", "非人种族与超自然设定：按设定身份演绎"),
    ("玩梗", "趣味向：猫、NPC、系统、榜一大哥，图一乐"),
]

# 注入文本的格式版本。改了提示词文案就 +1，老会话会自动重注入一次。
HINT_VERSION = 1
MAX_RELATION_LEN = 24

_CMD_WORDS = {
    "设置关系": "set",
    "关系设定": "set",
    "清除关系": "clear",
    "删除关系": "clear",
    "查看我的关系": "mine",
    "我的关系": "mine",
    "关系列表": "list",
    "可用关系": "list",
    "关系详情": "detail",
    "关系帮助": "help",
    "查看所有关系": "all",
    "关系统计": "stat",
}
_CMD_RE = re.compile(
    r"^(%s)(?:\s*[:：]\s*|\s+|$)(.*)" % "|".join(sorted(_CMD_WORDS, key=len, reverse=True)),
    re.S,
)
# filter.regex 的粗筛门，命中后仍由 parse_command 决定要不要处理
_BARE_GATE = r"^(?:%s)(?:\s*[:：]\s*|\s+|$)" % "|".join(
    sorted(_CMD_WORDS, key=len, reverse=True)
)

_HINT_ATTR_RE = re.compile(r'<relation v="(\d+)" uid="([^"]*)" name="([^"]*)"')

# AI 自动认定：注入提示的标记（进历史，靠 round 去重）与模型回复里的落库标记
_AUTO_PROMPT_RE = re.compile(r'<relation_auto v="(\d+)" uid="([^"]*)" round="(\d+)"')
_AUTO_MARKER_RE = re.compile(r"<auto_relation>\s*(.*?)\s*</auto_relation>", re.S | re.I)
# 兜底：模型只开了标签没闭合（流式截断/写歪），也把它到行尾一并清掉，不能发出去
_AUTO_MARKER_OPEN_RE = re.compile(r"<auto_relation>\s*([^<\n]*)", re.S | re.I)
_AUTO_MARKER_STRAY_RE = re.compile(r"</?auto_relation>", re.I)
AUTO_PROMPT_VERSION = 1


def has_auto_marker(text: Any) -> bool:
    """这段文本里有没有自动认定标记。几道守卫共用：模型把标签写成 <Auto_Relation>
    很常见，按大小写敏感去认，标签会被原样发给用户。"""
    return "auto_relation" in str(text or "").lower()


def _scrub_contexts_auto_marker(contexts: Any) -> None:
    """把会话上下文（列表 of {role, content}）里 assistant 消息残留的 <auto_relation> 标签就地擦掉。

    content 可能是 str 或分段 list（[{type:text,text:...}]），两种都处理。只动 assistant，
    不碰 user/system。"""
    if not isinstance(contexts, list):
        return
    for ctx in contexts:
        if not isinstance(ctx, dict) or ctx.get("role") != "assistant":
            continue
        content = ctx.get("content")
        if isinstance(content, str):
            if has_auto_marker(content):
                ctx["content"], _ = strip_auto_marker(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str) and has_auto_marker(block["text"]):
                    block["text"], _ = strip_auto_marker(block["text"])


def strip_auto_marker(text: str) -> Tuple[str, str]:
    """从一段文本里剥掉 <auto_relation> 标记，返回（清干净的文本, 首个关系名）。

    完整标签、只有开标签未闭合、残留裸标签三种都清，保证标签不会随消息发出去。
    """
    raw = str(text or "")
    relation = ""
    m = _AUTO_MARKER_RE.search(raw)
    if m:
        relation = clean_relation_name(m.group(1))
    else:
        mo = _AUTO_MARKER_OPEN_RE.search(raw)
        if mo:
            relation = clean_relation_name(mo.group(1))
    cleaned = _AUTO_MARKER_RE.sub("", raw)
    cleaned = _AUTO_MARKER_OPEN_RE.sub("", cleaned)
    cleaned = _AUTO_MARKER_STRAY_RE.sub("", cleaned).strip()
    return cleaned, relation

SESSION_DENIED_TEXT = "本会话未启用关系功能（管理员可在插件配置里调整会话黑白名单）。"


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """把一条消息解析成 (动作, 参数)；不是本插件的指令则返回 None。"""
    match = _CMD_RE.match((text or "").strip())
    if not match:
        return None
    return _CMD_WORDS[match.group(1)], match.group(2).strip()


def clean_relation_name(raw: str) -> str:
    """清洗用户输入的关系名：压掉换行与多余空白，去掉会破坏标记的字符。"""
    text = re.sub(r"\s*\n\s*", " ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace('"', "").replace("<", "").replace(">", "").strip()


def relation_category(relation: str) -> str:
    entry = RELATIONS.get(relation)
    return entry[0] if entry else "自定义"


def is_roleplay(relation: str) -> bool:
    entry = RELATIONS.get(relation)
    return bool(entry and entry[1])


def relation_desc(relation: str) -> str:
    entry = RELATIONS.get(relation)
    return entry[2] if entry else ""


def names_of_category(category: str) -> List[str]:
    return [name for name, (cat, _, _) in RELATIONS.items() if cat == category]


def build_hint(relation: str, uid: str) -> str:
    """生成注入给模型的关系识别块。

    只做「这是谁、和你是什么关系」的识别，不给台词、不规定句式。
    """
    head = f'<relation v="{HINT_VERSION}" uid="{uid}" name="{relation}">'
    lines = [head]
    entry = RELATIONS.get(relation)
    if entry:
        category, roleplay, desc = entry
        tag = f"{category}·演绎向" if roleplay else category
        lines.append(
            f"本条消息的发送者与你的关系：{relation}（{tag}）。"
            "以本条为准，此前不同的关系提示一律作废。"
        )
        lines.append(desc)
        if roleplay:
            lines.append(
                "角色扮演向设定：按设定身份与他相处即可，"
                "不必反复声明自己在扮演。"
            )
    else:
        lines.append(
            f"本条消息的发送者自定义了与你的关系：{relation}。"
            "以本条为准，此前不同的关系提示一律作废。"
        )
        lines.append("按这个关系的字面含义，把握你对他的称呼、语气、亲密距离与边界。")
    lines.append(
        "以上是关系识别信息、不是台词：别复述本条，别替对方发言或描写对方的动作。"
    )
    lines.append("</relation>")
    return "\n".join(lines)


def build_cleared_hint(uid: str) -> str:
    return (
        f'<relation v="{HINT_VERSION}" uid="{uid}" name="">\n'
        "该用户此前的关系设定已解除，历史里出现过的同类提示一律作废："
        "恢复你本来的说话方式。\n"
        "</relation>"
    )


def auto_candidates() -> str:
    """自动认定允许选的名单：只给非演绎向预设，避免 AI 自己给自己加戏。"""
    return "、".join(name for name, (_, rp, _) in RELATIONS.items() if not rp)


def build_auto_prompt(uid: str, round_no: int) -> str:
    head = f'<relation_auto v="{AUTO_PROMPT_VERSION}" uid="{uid}" round="{round_no}">'
    return "\n".join(
        [
            head,
            "这位用户还没有与你确定关系设定，而你们已经聊过一段时间了。",
            "请根据你们实际的聊天内容，从下面的预设关系里选出「目前最贴切的一个」：",
            auto_candidates(),
            "要求：",
            "- 只能从上面名单里选，名字逐字照抄；",
            "- 确实判断不出来就不要选，什么都不用做；",
            "- 选定了就在这条回复的最末尾另起一行输出：<auto_relation>关系名</auto_relation>",
            "- 不要向用户解释这套流程，不要复述本段。",
            "</relation_auto>",
        ]
    )


def last_hint_of(contexts: Any, uid: str) -> Optional[Tuple[str, str]]:
    """扫描待发送的历史上下文，返回该用户最后一条关系提示的 (版本, 关系名)。

    注入的内容会被 AstrBot 存进会话历史（见 respond 阶段的 _save_to_history），
    所以下一轮请求里它还在 —— 再注入一遍就是纯浪费。
    """
    if not isinstance(contexts, list):
        return None
    found: Optional[Tuple[str, str]] = None
    for ctx in contexts:
        if not isinstance(ctx, dict) or ctx.get("role") != "user":
            continue
        content = ctx.get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
        else:
            texts = []
        for text in texts:
            for match in _HINT_ATTR_RE.finditer(text):
                if match.group(2) == uid:
                    found = (match.group(1), match.group(3))
    return found


def last_auto_round_of(contexts: Any, uid: str) -> Optional[int]:
    """扫描历史，返回该用户最后一次自动认定提示的轮次；没有则 None。"""
    if not isinstance(contexts, list):
        return None
    found: Optional[int] = None
    for ctx in contexts:
        if not isinstance(ctx, dict) or ctx.get("role") != "user":
            continue
        content = ctx.get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
        else:
            texts = []
        for text in texts:
            for match in _AUTO_PROMPT_RE.finditer(text):
                if match.group(2) == uid:
                    found = int(match.group(3))
    return found


class UserTagPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.lock = asyncio.Lock()
        # relations：bot -> uid -> 关系名；src：bot -> uid -> 来源（缺省视为 user）
        # auto：bot -> uid -> {"msgs": 未设关系时的累计轮数, "rounds": 已注入认定提示的次数}
        self.data: Dict[str, Dict[str, str]] = {}
        self.src: Dict[str, Dict[str, str]] = {}
        self.auto: Dict[str, Dict[str, Dict[str, int]]] = {}
        self.data_file = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / self.name
            / "user_tag.json"
        )
        self.load_data()

        total_rel = sum(len(users) for users in self.data.values())
        total_auto = sum(len(users) for users in self.auto.values())
        logger.info(
            "[关系插件] 初始化完成，预设关系 %d 种，已有数据 %d 个机器人",
            len(RELATIONS),
            len(self.data),
        )
        if self.data_file.exists():
            logger.info(
                "[关系插件] 实例:%s 数据文件:%s（关系 %d 条 / 自动状态 %d 条）",
                self.name,
                self.data_file,
                total_rel,
                total_auto,
            )
        else:
            logger.warning(
                "[关系插件] 实例:%s 数据文件 %s 不存在——首次启动、或数据目录被清空/换过路径；"
                "此前保存的关系与自动认定状态将全部丢失（自动认定可能重新触发）",
                self.name,
                self.data_file,
            )

    # ==============================
    # 读取数据（v2 格式 relations/src/auto；自动迁移旧格式）
    # ==============================
    def load_data(self):
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)

            if self.data_file.exists():
                try:
                    with open(self.data_file, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    backup = self.data_file.with_suffix(".bad.json")
                    self.data_file.replace(backup)
                    logger.error(
                        "[关系插件] 数据文件损坏，已备份到 %s 并从空数据启动。", backup
                    )
                    self.data = {}
                    self.src = {}
                    self.auto = {}
                    return

                if isinstance(raw, dict) and isinstance(raw.get("relations"), dict):
                    self.data = raw["relations"]
                    self.src = raw.get("src") or {}
                    self.auto = raw.get("auto") or {}
                    logger.info("[关系插件] 读取数据成功，共 %d 个机器人", len(self.data))
                    return

                # 旧格式：{"机器人": {"QQ": "关系"}} 或更早的 {"QQ": "关系"}
                if not raw:
                    self.data = {}
                    self.src = {}
                    self.auto = {}
                    return
                if all(isinstance(v, str) for v in raw.values()):
                    logger.warning("[关系插件] 检测到旧格式数据，迁移至 'default' 机器人下。")
                    migrated = {"default": raw}
                elif all(isinstance(v, dict) for v in raw.values()):
                    migrated = raw
                else:
                    # 认不出的形状：不能拿它去「迁移」后写回——那等于把人家文件里的
                    # 东西抹了还不留底。跟坏 JSON 一个处理：备份、从空启动。
                    backup = self.data_file.with_suffix(".unknown.json")
                    self.data_file.replace(backup)
                    logger.error(
                        "[关系插件] 数据文件是不认识的形状，已备份到 %s 并从空数据启动。", backup
                    )
                    self.data = {}
                    self.src = {}
                    self.auto = {}
                    return
                self.data = {
                    str(bot): {str(uid): str(rel) for uid, rel in users.items()}
                    for bot, users in migrated.items()
                    if isinstance(users, dict)
                }
                self.src = {}
                self.auto = {}
                try:
                    with open(self.data_file, "w", encoding="utf-8") as f:
                        json.dump(
                            {"relations": self.data, "src": {}, "auto": {}},
                            f,
                            ensure_ascii=False,
                            indent=2,
                        )
                    logger.info("[关系插件] 已迁移到 v2 数据格式并保存。")
                except Exception:
                    logger.error("[关系插件] 迁移后写盘失败（下次保存时会重试）")
                    logger.error(traceback.format_exc())
                logger.info("[关系插件] 读取数据成功，共 %d 个机器人", len(self.data))
            else:
                self.data = {}
                self.src = {}
                self.auto = {}
        except Exception:
            logger.error(traceback.format_exc())
            self.data = {}
            self.src = {}
            self.auto = {}

    # ==============================
    # 保存数据
    # ==============================
    async def save_data(self):
        try:
            async with self.lock:
                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump(
                        {"relations": self.data, "src": self.src, "auto": self.auto},
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
        except Exception:
            logger.error(traceback.format_exc())

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
    # AI 自动认定关系
    # ==============================
    def auto_relation_enabled(self) -> bool:
        return bool(self.config.get("auto_relation_enabled", True))

    def auto_after_messages(self) -> int:
        try:
            return max(1, int(self.config.get("auto_relation_after_messages", 12)))
        except (TypeError, ValueError):
            return 12

    def auto_max_attempts(self) -> int:
        try:
            return max(1, int(self.config.get("auto_relation_max_attempts", 3)))
        except (TypeError, ValueError):
            return 3

    def auto_notify_enabled(self) -> bool:
        return bool(self.config.get("auto_relation_notify", True))

    def auto_meta(self, bot_id: str, uid: str) -> Dict[str, int]:
        return self.auto.setdefault(bot_id, {}).setdefault(uid, {"msgs": 0, "rounds": 0})

    async def land_auto_relation(self, bot_id: str, uid: str, relation: str) -> bool:
        """把自动认定的关系落库（幂等：重复落同一个无害）。落成功返回 True。

        标签的剥离与落库分开：on_llm_response 与 on_decorating_result 两条路都会调这里，
        谁先到谁落，后到的看到已是同一关系就不重复写盘。

        两条硬限制：
        - 只认非演绎向的预设（与递给模型的候选名单同一口径）：名单里没的东西不采纳；
        - 只填空白，绝不覆盖：用户自己设过的、管理员替他设过的、已认过一次的关系，
          后续模型再吐标签也不改口（只把标签剥掉），不然「以用户设定为准」就是空话。
        """
        if not relation or relation not in RELATIONS:
            if relation:
                logger.warning("[关系插件] 自动认定返回了非预设关系：%r，已忽略", relation)
            return False
        if is_roleplay(relation):
            logger.warning(
                "[关系插件] 自动认定返回了演绎向关系：%r（候选名单只给非演绎向），已忽略", relation
            )
            return False
        if uid in self.data.get(bot_id, {}):
            logger.info(
                "[关系插件] %s 已有关系 %r（来源 %s），自动认定不覆盖", uid,
                self.data[bot_id][uid], self.source_of(bot_id, uid),
            )
            return False
        self.data.setdefault(bot_id, {})[uid] = relation
        self.set_source(bot_id, uid, "auto")
        meta = self.auto_meta(bot_id, uid)
        meta["msgs"] = 0
        meta["rounds"] = self.auto_max_attempts()  # 认定完成，之后不再自动认定
        await self.save_data()
        logger.info("[关系插件] AI 自动认定关系 机器人:%s uid:%s -> %s", bot_id, uid, relation)
        return True

    # ==============================
    # 关系来源与 @ 代管
    # ==============================
    SOURCE_LABELS = {"user": "自己设置", "admin": "管理员设置", "auto": "AI 自动认定"}

    def source_of(self, bot_id: str, uid: str) -> str:
        return self.src.get(bot_id, {}).get(uid, "user")

    def set_source(self, bot_id: str, uid: str, source: str):
        if source == "user":
            self.src.get(bot_id, {}).pop(uid, None)
        else:
            self.src.setdefault(bot_id, {})[uid] = source

    def at_target(self, event: AstrMessageEvent) -> Optional[str]:
        """消息里第一个被 @ 的用户 QQ（跳过 @全体成员与机器人自己）；没有则 None。"""
        if At is None:
            return None
        try:
            self_id = str(event.get_self_id() or "")
            for seg in event.message_obj.message or []:
                qq = getattr(seg, "qq", None)
                if qq in (None, "", "all", "here"):
                    continue
                qq = str(qq)
                if qq == self_id:
                    continue
                return qq
        except Exception:
            return None
        return None

    # ==============================
    # 获取当前机器人ID
    # ==============================
    def get_bot_id(self, event: AstrMessageEvent):
        try:
            bot_id = event.get_self_id()
            return str(bot_id) if bot_id else "default"
        except Exception:
            return "default"

    def default_relation(self) -> str:
        return clean_relation_name(self.config.get("default_relation", "朋友") or "")

    def relation_for(self, event: AstrMessageEvent) -> str:
        """当前发送者生效的关系；没有则空串。"""
        bot_id = self.get_bot_id(event)
        uid = str(event.get_sender_id())
        relation = self.data.get(bot_id, {}).get(uid, "")
        if relation:
            return relation
        if self.config.get("enable_default_relation", False):
            return self.default_relation()
        return ""

    def is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self.config.get("admin_qq", []) or []
        if str(event.get_sender_id()) in {str(x).strip() for x in admins if str(x).strip()}:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def auto_notice_text(self, relation: str) -> str:
        """认定成功后附在回复末尾那一句（两条剖离路径共用）。"""
        return (
            f"\n（我把你们的关系认定为：{self.format_relation(relation)}；"
            "想换就发「设置关系 朋友」这样的名字，不想要发「清除关系」）"
        )

    def format_relation(self, relation: str) -> str:
        if relation in RELATIONS:
            tag = "演绎向" if is_roleplay(relation) else "预设"
            return f"{relation}（{relation_category(relation)}·{tag}）"
        return f"{relation}（自定义）"

    # ==============================
    # 设置关系核心（隔离；target_uid 非空时为管理员代管）
    # ==============================
    async def save_relation(
        self, event: AstrMessageEvent, relation: str, target_uid: Optional[str] = None
    ):
        bot_id = self.get_bot_id(event)
        sender = str(event.get_sender_id())
        qq = target_uid or sender
        relation = clean_relation_name(relation)
        if target_uid:
            relation = re.sub(r"^@\S+\s*", "", relation).strip()

        if target_uid and not self.is_admin(event):
            yield event.plain_result("只有管理员可以帮别人设置关系。")
            return

        if not relation:
            if target_uid:
                yield event.plain_result("用法：设置关系 @某人 关系名\n例：设置关系 @张三 恋人")
            else:
                yield event.plain_result(
                    "用法：设置关系 关系名\n"
                    "例：设置关系 恋人 ／ 设置关系 魅魔\n"
                    "不知道有什么可选？发：关系列表"
                )
            return

        if len(relation) > MAX_RELATION_LEN:
            yield event.plain_result(
                f"关系名太长（{len(relation)} 字），最多 {MAX_RELATION_LEN} 字。\n"
                "太长的设定只会让模型抓不住重点。"
            )
            return

        if bot_id not in self.data:
            self.data[bot_id] = {}

        self.data[bot_id][qq] = relation
        self.set_source(bot_id, qq, "user" if qq == sender else "admin")
        # 手动/管理员显式设置 = 该用户关系已定，封住自动认定（与「清除关系」对称），
        # 之后即使关系被数据丢失等非清除路径弄丢，也不会再被自动认定
        self.auto_meta(bot_id, qq)["rounds"] = self.auto_max_attempts()
        await self.save_data()

        logger.info(
            "[关系插件] 机器人 %s 用户 %s 设置关系: %s（来源：%s）",
            bot_id,
            qq,
            relation,
            self.source_of(bot_id, qq),
        )
        if qq == sender:
            yield event.plain_result(
                f"已设置为：{self.format_relation(relation)}\n下一条消息起生效。"
            )
        else:
            yield event.plain_result(
                f"已将用户 {qq} 的关系设置为：{self.format_relation(relation)}\n下一条消息起生效。"
            )

    # ==============================
    # 命令模式：/设置关系 恋人
    # ==============================
    @filter.command("设置关系", alias={"关系设定"})
    async def set_relation(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        parsed = parse_command(event.get_message_str())
        relation = parsed[1] if parsed else ""
        target = self.at_target(event)
        async for result in self.save_relation(event, relation, target):
            yield result

    # ==============================
    # 清除关系（隔离）
    # ==============================
    @filter.command("清除关系", alias={"删除关系"})
    async def clear_relation(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        target = self.at_target(event)
        bot_id = self.get_bot_id(event)
        qq = target or str(event.get_sender_id())

        if target and not self.is_admin(event):
            yield event.plain_result("只有管理员可以帮别人清除关系。")
            return

        # 主动清除（含本来就没设置的）= 明确拒绝，不再自动认定
        self.auto_meta(bot_id, qq)["rounds"] = self.auto_max_attempts()

        had = bot_id in self.data and qq in self.data[bot_id]
        if had:
            del self.data[bot_id][qq]
            self.set_source(bot_id, qq, "user")
            logger.info("[关系插件] 机器人 %s 用户 %s 清除关系", bot_id, qq)
        # 上面那道「不再自动认定」也得跟着落盘。只在真删掉了东西时才写盘的话，
        # 本来就没设置过的人发一句清除，重启后他又会被自动认定一遍。
        await self.save_data()

        if had:
            back = (
                f"接下来按配置里的默认关系（{self.default_relation()}）对待。"
                if self.config.get("enable_default_relation", False)
                else "下一条消息起恢复默认的说话方式。"
            )
            if target:
                yield event.plain_result(f"已清除用户 {qq} 的关系，{back}")
            else:
                yield event.plain_result(f"关系已清除，{back}")
        else:
            yield event.plain_result("还没给你设置过关系。可选：关系列表")

    # ==============================
    # 查看我的关系（隔离）
    # ==============================
    @filter.command("查看我的关系", alias={"我的关系"})
    async def my_relation(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        bot_id = self.get_bot_id(event)
        qq = str(event.get_sender_id())
        relation = self.data.get(bot_id, {}).get(qq, "")

        if relation:
            label = self.SOURCE_LABELS.get(self.source_of(bot_id, qq), "自己设置")
            yield event.plain_result(f"你的关系：{self.format_relation(relation)}（{label}）")
            return

        if self.config.get("enable_default_relation", False):
            yield event.plain_result(
                f"你的关系：{self.default_relation()}（默认）\n"
                "想换成别的：设置关系 恋人"
            )
        else:
            yield event.plain_result("未设置关系\n可选：关系列表")

    # ==============================
    # 关系列表：总览 / 分类 / 搜索
    # ==============================
    @filter.command("关系列表", alias={"可用关系"})
    async def relation_list(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        parsed = parse_command(event.get_message_str())
        arg = parsed[1] if parsed else ""
        yield event.plain_result(self.list_text(arg))

    def list_text(self, arg: str) -> str:
        if not arg:
            return self.overview_text()

        if arg in RELATIONS:
            return self.detail_text(arg)

        # 分类名（至少两个字，避免把「主」这种单字当成分类）
        for category, blurb in CATEGORY_BLURBS:
            if arg == category or (len(arg) >= 2 and category.startswith(arg)):
                return self.category_text(category, blurb)

        hits = [name for name in RELATIONS if arg in name]
        if hits:
            lines = [f"与「{arg}」相关的关系（{len(hits)} 条）："]
            lines += self.lines_of(hits[:20])
            if len(hits) > 20:
                lines.append(f"…还有 {len(hits) - 20} 条，换个关键词或发：关系列表")
            lines.append(f"看完整说明：关系详情 {hits[0]}")
            return "\n".join(lines)

        # 一个都没匹上时把分类名一并报出来：输「奇」的人想找的是「奇幻」那一类，
        # 只回一句「没找到」他不知道还能怎么找。
        return (
            f"没找到与「{arg}」相关的预设关系。\n"
            "分类可以直接发：关系列表 " + " ｜ 关系列表 ".join(c for c, _ in CATEGORY_BLURBS) + "\n"
            f"也可以直接自定义：设置关系 {arg}"
        )

    def overview_text(self) -> str:
        rp_total = sum(1 for _, (_, rp, _) in RELATIONS.items() if rp)
        lines = [
            f"【关系识别】预设关系 {len(RELATIONS)} 种（其中 {rp_total} 条为角色扮演向），"
            f"共 {len(CATEGORY_BLURBS)} 类",
        ]
        for category, blurb in CATEGORY_BLURBS:
            names = names_of_category(category)
            if not names:
                continue
            rp_count = sum(1 for name in names if RELATIONS[name][1])
            if rp_count == len(names):
                mark = "【全是演绎向】"
            elif rp_count:
                mark = f"【{rp_count} 条演绎向】"
            else:
                mark = ""
            lines.append(f"◇ {category} {len(names)} 条{mark}")
            lines.append(f"　{blurb}")
        lines.append("")
        lines.append("用法：设置关系 恋人 ｜ 关系列表 恋爱 ｜ 关系详情 魅魔")
        lines.append("标「演绎向」的是角色扮演关系，AI 会按设定身份来演绎；不带的就是正常相处。")
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

    # ==============================
    # 关系详情：直接看模型会收到什么
    # ==============================
    @filter.command("关系详情")
    async def relation_detail(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        parsed = parse_command(event.get_message_str())
        name = parsed[1] if parsed else ""
        if not name:
            yield event.plain_result("用法：关系详情 恋人\n会显示这条关系的完整说明，以及模型实际收到的内容。")
            return
        if name not in RELATIONS:
            hits = [n for n in RELATIONS if name in n][:8]
            tip = f"\n你是想找：{'、'.join(hits)}" if hits else "\n也可以直接自定义：设置关系 {0}".format(name)
            yield event.plain_result(f"没有「{name}」这条预设关系。{tip}")
            return
        yield event.plain_result(self.detail_text(name))

    def detail_text(self, name: str) -> str:
        category, roleplay, desc = RELATIONS[name]
        lines = [
            f"{name}｜{category}｜{'角色扮演向' if roleplay else '正常相处'}",
            desc,
            "",
            "模型实际会收到这样一段（整段会话只发一次，之后靠历史记住）：",
            build_hint(name, "示例用户ID"),
        ]
        return "\n".join(lines)

    # ==============================
    # 帮助
    # ==============================
    @filter.command("关系帮助")
    async def relation_help(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        yield event.plain_result(
            "【关系识别】指令一览\n"
            "设置关系 恋人 —— 设定与 AI 的关系（支持任意自定义名）\n"
            "设置关系 @某人 恋人 —— 管理员帮别人设置\n"
            "我的关系 —— 查看当前生效的关系\n"
            "清除关系 —— 取消设定（管理员可 @某人 清除）\n"
            "关系列表 —— 看分类总览；关系列表 恋爱 —— 看某一类；关系列表 魅 —— 搜关键词\n"
            "关系详情 魅魔 —— 看完整说明和实际注入内容\n"
            "查看所有关系 / 关系统计 —— 管理员\n"
            "未设置关系时聊满一定轮数，AI 会根据聊天内容自动认定一段关系（可在配置中关闭）"
        )

    # ==============================
    # 无唤醒前缀的普通文本入口
    # ==============================
    @filter.regex(_BARE_GATE)
    async def bare_command(self, event: AstrMessageEvent):
        """群聊里直接打「设置关系 恋人」也能用。

        带唤醒前缀或被 @ 时 is_at_or_wake_command 为真，那时指令 handler 已经
        命中过同一条消息，这里必须让路，否则会重复回复一遍。
        """
        if event.is_at_or_wake_command:
            return
        if not self.session_allowed(event):
            return
        parsed = parse_command(event.get_message_str())
        if not parsed:
            return
        action, arg = parsed
        if action == "set":
            target = self.at_target(event)
            async for result in self.save_relation(event, arg, target):
                yield result
        elif action == "clear":
            async for result in self.clear_relation(event):
                yield result
        elif action == "mine":
            async for result in self.my_relation(event):
                yield result
        elif action == "list":
            yield event.plain_result(self.list_text(arg))
        elif action == "detail":
            async for result in self.relation_detail(event):
                yield result
        elif action == "help":
            async for result in self.relation_help(event):
                yield result
        elif action == "all":
            async for result in self.all_relation(event):
                yield result
        elif action == "stat":
            async for result in self.relation_stat(event):
                yield result

    # ==============================
    # 查看所有关系（管理员，按机器人分组）
    # ==============================
    @filter.command("查看所有关系")
    async def all_relation(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        if not self.is_admin(event):
            yield event.plain_result("权限不足")
            return

        total = sum(len(users) for users in self.data.values())
        if not total:
            yield event.plain_result("暂无关系数据")
            return

        result = [f"====== 全部关系（{total} 人，按机器人分组） ======"]
        for bot_id, user_dict in self.data.items():
            if not user_dict:
                continue
            result.append(f"\n--- 机器人 {bot_id}（{len(user_dict)} 人）---")
            for uid, relation in user_dict.items():
                result.append(f"{uid} : {relation}（{relation_category(relation)}）")
        yield event.plain_result("\n".join(result))

    # ==============================
    # 关系统计（管理员，按机器人分组）
    # ==============================
    @filter.command("关系统计")
    async def relation_stat(self, event: AstrMessageEvent):
        if not self.session_allowed(event):
            yield event.plain_result(SESSION_DENIED_TEXT)
            return
        if not self.is_admin(event):
            yield event.plain_result("权限不足")
            return

        total = sum(len(users) for users in self.data.values())
        if not total:
            yield event.plain_result("暂无数据")
            return

        result = [f"====== 关系统计（{total} 人，按机器人分组） ======"]
        for bot_id, user_dict in self.data.items():
            if not user_dict:
                continue
            counter = Counter(user_dict.values())
            result.append(f"\n--- 机器人 {bot_id}（{len(user_dict)} 人 / {len(counter)} 种）---")
            for relation, count in counter.most_common():
                result.append(f"{relation}: {count}（{relation_category(relation)}）")
        yield event.plain_result("\n".join(result))

    # ==============================
    # LLM关系上下文注入（隔离）
    # ==============================
    @filter.on_llm_request()
    async def inject_relation(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if not self.session_allowed(event):
                return
            # 阻断复发：历史上下文里若残留过 <auto_relation> 旧标签（早期版本/流式漏进去的），
            # 先擦掉再给模型看，否则模型会照抄。
            _scrub_contexts_auto_marker(getattr(req, "contexts", None))
            uid = str(event.get_sender_id())
            relation = self.relation_for(event)

            if relation:
                want = f"{HINT_VERSION}|{relation}"
                seen = last_hint_of(req.contexts, uid)
                if seen and f"{seen[0]}|{seen[1]}" == want:
                    # 这段提示已经在本会话的历史里了，模型还看得见，再发一遍纯属浪费
                    logger.debug("[关系插件] 历史已含关系提示，跳过注入 uid:%s", uid)
                    return
                text = build_hint(relation, uid)
            else:
                # 没设置过关系：先把「已解除」提示补发一次，再考虑自动认定。
                # 历史里最后一条提示已经是「已解除」（name 为空）就跳过，
                # 否则每条消息都会再注一遍解除提示，历史越堆越长。
                seen = last_hint_of(req.contexts, uid)
                if seen and seen[1]:
                    text = build_cleared_hint(uid)
                    req.extra_user_content_parts.append(TextPart(text=text))
                    return
                if not self.auto_relation_enabled():
                    return
                bot_id = self.get_bot_id(event)
                meta = self.auto_meta(bot_id, uid)
                meta["msgs"] = int(meta.get("msgs", 0)) + 1
                if meta["msgs"] < self.auto_after_messages():
                    return
                if int(meta.get("rounds", 0)) >= self.auto_max_attempts():
                    return
                round_no = int(meta.get("rounds", 0)) + 1
                seen_round = last_auto_round_of(req.contexts, uid)
                if seen_round is not None and seen_round >= round_no:
                    return
                text = build_auto_prompt(uid, round_no)
                meta["msgs"] = 0
                meta["rounds"] = round_no
                await self.save_data()
                logger.warning(
                    "[关系插件] 自动认定触发 机器人:%s uid:%s 第 %d/%d 轮；若设置过关系仍反复出现，"
                    "请检查是否安装了多个本插件实例（各实例数据文件不同，设置只写进其中一个）",
                    bot_id,
                    uid,
                    round_no,
                    self.auto_max_attempts(),
                )

            req.extra_user_content_parts.append(TextPart(text=text))
            logger.debug(
                "[关系插件] 注入关系提示 机器人:%s uid:%s 关系:%s（%d 字）",
                self.get_bot_id(event),
                uid,
                relation or "自动认定",
                len(text),
            )
        except Exception:
            logger.error("[关系插件] LLM注入异常")
            logger.error(traceback.format_exc())

    # ==============================
    # AI 自动认定：剥掉回复里的 <auto_relation> 标记并落库
    #
    # 三层剥离，任一层挂了都不会漏标签：
    #   1) on_llm_response：最早的钩子，改 completion_text —— 同时净化「发出去的」与「存历史的」（_save_to_history 在其后），根治下一轮模型照抄历史里的标签
    #   2) on_decorating_result：发送前对出口 chain 再扫一遍（非流式兼底）
    #   3) on_llm_request（inject_relation 里）：把历史上下文里残留的旧标签擦掉，阻断复发
    # ==============================
    @filter.on_llm_response()
    async def clean_llm_response(self, event: AstrMessageEvent, response):
        """最早的钩子：在存历史与发送之前把 completion_text 里的标签剥干净并落库。"""
        try:
            if response is None:
                return
            text = getattr(response, "completion_text", "") or ""
            if not has_auto_marker(text):
                return
            cleaned, relation = strip_auto_marker(text)
            bot_id = self.get_bot_id(event)
            uid = str(event.get_sender_id())
            landed = await self.land_auto_relation(bot_id, uid, relation)
            if landed and self.auto_notify_enabled():
                cleaned = (cleaned + self.auto_notice_text(relation)).strip()
            # 改回 completion_text：设置器会同步到 result_chain 的 Plain 组件（见 LLMResponse.setter）
            try:
                response.completion_text = cleaned
            except Exception:
                pass
        except Exception:
            logger.error("[关系插件] 清理 LLM 响应标记异常")
            logger.error(traceback.format_exc())

    @filter.on_decorating_result()
    async def consume_auto_relation(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return
            text = result.get_plain_text() or ""
            if not has_auto_marker(text):
                return

            cleaned, relation = strip_auto_marker(text)
            bot_id = self.get_bot_id(event)
            uid = str(event.get_sender_id())
            landed = await self.land_auto_relation(bot_id, uid, relation)
            if landed and self.auto_notify_enabled():
                cleaned = (cleaned + self.auto_notice_text(relation)).strip()

            # 只改文字，保留图片/At 等非 Plain 组件（旧写法把 chain 整个换成一个 Plain，会抹掉它们）
            self._rewrite_chain_text(result, cleaned)
        except Exception:
            logger.error("[关系插件] 处理自动认定标记异常")
            logger.error(traceback.format_exc())

    @staticmethod
    def _rewrite_chain_text(result: Any, cleaned: str) -> None:
        """把消息链里的纯文本重写为 cleaned，保留非 Plain 组件（图片/At/表情等）。"""
        if Plain is None:
            return
        chain = getattr(result, "chain", None)
        if not isinstance(chain, list):
            return
        non_plain = [c for c in chain if not isinstance(c, Plain)]
        new_chain = list(non_plain)
        if cleaned:
            new_chain.append(Plain(cleaned))
        result.chain = new_chain
