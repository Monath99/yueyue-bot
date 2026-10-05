import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
import datetime
import json
import os
import re

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- 月月自身数据（原封不动） ---
bot_data = {
    "hunger": 50, "thirst": 50, "energy": 50, "affection": 0,
    "thoughts": [
        "想吃小鱼干", "想吃三色丸子", "想吃鲷鱼烧", "想吃金枪鱼罐头",
        "想吃猫薄荷饼干", "想吃章鱼烧", "想吃奶油泡芙", "想吃烤秋刀鱼",
        "想吃草莓大福", "想吃鲜虾寿司", "想吃鱼骨饼干", "想吃小虾干",
        "想吃糙米鱼饼", "想吃烤鱼串", "想被月么呐摸摸头", "想给萌新指路", 
        "想睡觉", "想玩触手打结"
    ]
}

pat_cooldown = {}
feed_cooldown = {}
milk_tea_waiting = {}

# ================= @月月 的梗彩蛋 =================
# 想加梗就往这个表里塞：(关键词, (回复1, 回复2, ...))
MEME_EGGS = [
    (("v我50", "v50", "疯狂星期四", "kfc"), (
        "本座堂堂触手霸主，居然沦落到给你凑五十块炸鸡钱？行吧本座掏了，但鸡腿归本座。🍗",
        "今天星期四，本座的钱包比脸还干净。💸",
    )),
    (("退退退", "退！退！退", "退！退！退！"), (
        "退！退！退！本座的触手比你长！🐙",
        "本座也退，退到小鱼干堆里不出来。🐟",
    )),
    (("泰裤辣", "太酷啦"), (
        "泰裤辣！就是泰裤辣！本座的触手也泰裤辣！✨",
        "泰裤辣到本座八条触手当场打了个中国结。💦",
    )),
    (("遥遥领先",), (
        "遥遥领先！本座摸鱼的速度遥遥领先全服桌宠。🏆",
        "论吃小鱼干，本座遥遥领先，第二名连尾灯都看不到。🐟",
    )),
    (("摆烂", "开摆", "躺平"), (
        "摆烂是吧？本座陪你。触手一摊，谁也不许努力。🛌",
        "本座早就躺好了，你怎么才来。🛌",
    )),
    (("唱首歌", "唱歌", "唱一个"), (
        "本座是条小章鱼，八条腿都很调皮♪ …好听吗？好听就是好歌。🎤",
        "本座唱歌不看音准，看心情。🎤",
    )),
    (("跳个舞", "跳舞", "舞一个"), (
        "本座给你跳一个！触手打结舞！…哎呀，缠住自己了。💦",
        "节目名：《八爪乱舞》，表演者：本座。🐙",
    )),
    (("晚安", "睡了", "睡觉了", "去睡了"), (
        "晚安！本座把触手都收好，就睡在你枕头边上。🌙",
        "快去睡，本座守着你的梦。谁在梦里欺负你，本座咬他。🦊",
    )),
    (("早安", "早上好"), (
        "早上好！本座已经醒了八分钟了，一直在等你叫本座。🌞",
        "早！本座把小鱼干给你留着呢。🐟",
    )),
    (("生日快乐",), (
        "生日快乐！本座给你唱生日歌，跑调的那种，但心意是真的。🎂",
        "生日快乐！许愿的时候记得把本座也算进去。🎂",
    )),
    (("emo了", "好emo", "难过", "不开心", "心情不好"), (
        "呜…谁惹你了？本座用八条触手把他缠起来。先抱一下。🫂",
        "本座把耳朵借给你摸，摸到你不难受为止。🦊",
    )),
    (("不想上班", "不想打工", "想辞职", "上班好累"), (
        "懂！本座也不想上班。给，小鱼干一根，吃完了再当牛马。🐟",
        "上班？本座只会上桌。🐙",
    )),
    (("你是ai吗", "是不是机器人", "你是机器人"), (
        "本座是章鱼！是狐狸！是月月！才不是什么AI…本座会咬人的。🦊",
        "AI有本座这么好吃吗？🐟",
    )),
    (("瑟瑟", "色色", "nsfw"), (
        "嘘！本座捂眼睛了！…指缝张得有点大而已。🙈",
        "本座还小，听不懂你在说什么。🙈",
    )),
    (("你是什么生物", "猫还是狐狸", "狐狸还是章鱼"), (
        "本座是章鱼！…不对，本座是狐狸！…好吧，本座全都要。🦊🐙",
        "别问了，本座自己也没搞明白。💕",
    )),
]


# ================= 算卦：六十四卦全 =================
# 按周易本经顺序（第 1 卦到第 64 卦），每卦配一句月月的话
HEXAGRAMS = [
    ("乾为天", "本座看今天气势特别足！想做的事就大胆去做，别缩着。"),
    ("坤为地", "今天适合稳稳当当，别逞强，厚着脸皮慢慢来反而顺。"),
    ("水雷屯", "开头会有点磕磕绊绊，别急着下结论，先熬过这一阵。"),
    ("山水蒙", "今天容易犯迷糊，遇到不懂的就问，别硬猜。"),
    ("水天需", "该等的时候就等，今天的运气在耐心这一边。"),
    ("天水讼", "今天别跟人争对错，赢了道理输了心情，不划算。"),
    ("地水师", "今天做事要有章法，先排好顺序再动手。"),
    ("水地比", "今天适合找人搭伙，一个人硬扛不如拉个帮手。"),
    ("风天小畜", "小有积蓄的一天，别乱花钱，攒着有用。"),
    ("天泽履", "今天走路小心点，话说软一点，能少踩很多坑。"),
    ("地天泰", "顺！今天做什么都通气，趁势把拖着的事办掉。"),
    ("天地否", "今天有点堵，别硬闯，先歇口气，明天会好。"),
    ("天火同人", "今天适合跟人合作，遇到同频的人别错过。"),
    ("火天大有", "今天收获不小，但记得分一点给别人，好运才留得住。"),
    ("地山谦", "低调一点反而走运，今天别把自己抬太高。"),
    ("雷地豫", "今天心情会不错，适合放松一下，别绷着。"),
    ("泽雷随", "今天顺着来，别硬拧，跟着感觉走反而顺。"),
    ("山风蛊", "有件旧事该收拾了，今天动手整理正好。"),
    ("地泽临", "今天适合看远一点，别只盯着眼前这点小事。"),
    ("风地观", "今天多听多看少说话，你会看到平时没注意的东西。"),
    ("火雷噬嗑", "今天有块硬骨头，咬着牙啃下去就通了。"),
    ("山火贲", "今天适合把自己收拾漂亮点，好心情会跟着来。"),
    ("山地剥", "今天不太顺，别做大决定，守住手里的就好。"),
    ("地雷复", "低谷到头了，今天开始慢慢回血，别急。"),
    ("天雷无妄", "今天别乱来，老老实实做事，运气自然跟上来。"),
    ("山天大畜", "今天适合攒本事攒资源，厚积才有薄发。"),
    ("山雷颐", "今天好好吃饭好好睡觉，养足精神比什么都强。"),
    ("泽风大过", "今天有点超出负荷了，该减就减，别硬撑。"),
    ("坎为水", "今天水有点深，谨慎一点，别蹚不熟的路。"),
    ("离为火", "今天脑子亮堂，适合想事情、说清楚话。"),
    ("泽山咸", "今天容易心动，缘分这东西来了就接着。"),
    ("雷风恒", "今天贵在坚持，别三天打鱼两天晒网。"),
    ("天山遁", "今天该退就退，躲一躲不是怂，是聪明。"),
    ("雷天大壮", "今天力气足，但别用力过猛，收着点。"),
    ("火地晋", "今天往上走，适合主动争取，会被看见。"),
    ("地火明夷", "今天光被遮住了，先低调藏着，等天晴。"),
    ("风火家人", "今天适合陪陪身边的人，家里的事优先。"),
    ("火泽睽", "今天看谁都不太顺眼？先别急着翻脸，各想各的也正常。"),
    ("水山蹇", "今天路有点难走，绕一下比硬闯强。"),
    ("雷水解", "今天结的疙瘩会松开，该说的话说开就好。"),
    ("山泽损", "今天舍一点反而得更多，别算太精。"),
    ("风雷益", "今天越给越有，帮别人一把自己也会顺。"),
    ("泽天夬", "今天该断的断干净，拖泥带水最伤运。"),
    ("天风姤", "今天可能遇到个意外的人，保持礼貌就好。"),
    ("泽地萃", "今天适合聚一聚，人多的地方有好消息。"),
    ("地风升", "今天一步一个台阶，慢慢往上爬最稳。"),
    ("泽水困", "今天有点紧，别慌，先把眼前的窟窿补上。"),
    ("水风井", "今天适合回到老地方、找老朋友，会有收获。"),
    ("泽火革", "今天适合换个做法，旧办法已经不太灵了。"),
    ("火风鼎", "今天适合把新东西做起来，本座看好你。"),
    ("震为雷", "今天动静不小，别被吓到，震完了就晴了。"),
    ("艮为山", "今天适合停下来，什么都不做也是一种答案。"),
    ("风山渐", "今天慢慢来，快反而容易摔，稳着走。"),
    ("雷泽归妹", "今天有点小情绪，别在最上头的时候做决定。"),
    ("雷火丰", "今天场面很旺，享受就好，别飘。"),
    ("火山旅", "今天可能出门奔波，随身东西看紧点。"),
    ("巽为风", "今天顺着风走，柔一点反而能钻进去。"),
    ("兑为泽", "今天嘴巴甜一点，好消息都是从聊天里来的。"),
    ("风水涣", "今天适合把堵着的东西散开，该忘的忘掉。"),
    ("水泽节", "今天收着点，无论是钱还是脾气，都别透支。"),
    ("风泽中孚", "今天诚信最值钱，说到做到会有回报。"),
    ("雷山小过", "今天小事可以随意，大事千万别冒险。"),
    ("水火既济", "今天事情已经成了大半，收好尾别松懈。"),
    ("火水未济", "今天还没到终点，别急着庆祝，再走两步。"),
]

# ================= 抽卡：角色池 =================
# 稀有度只是标签，抽卡时所有角色等概率
UR_RAW = "秦江、王观山、齐枫、贺玉啸、陈离巽、江河、拓野、白羽礼、顾清风、姬明渊"
SSR_RAW = "阿良、安河、白城风、白面、白祈、白玉、柏生、卞无归、苍不问、长生、苍素枯、朝云、沉一倬、沉璟、陈彻、陈火风、陈念、陈天乐、楚观、春山、段坟、段惊砚、段羽、杜王侯、厄洛斯塔、范有定、风尘、风千里、风去来、风若离、傅云舟、顾淮、顾天涯、关城、关渊、韩彰、花千芳、花弑、花无端、华宴、霍灾、姬濯、纪梁、纪梦言、季白辞、季三郎、季无痕、江凉玉、江无命、江寻、蒋昭明、景行、卡尼尔、空闻、孔摇凤、克厄斯、冷秋风、李三百、李长安、李自闲、林落、林栩、凌楚、凌折兰、刘青春、楼千、陆离、陆千秋、鹿悠、洛洛子、落无痕、玛卡、孟无忧、木栖、木朽胜、宁采臣、宁杰、裴不留、裴江川、裴无己、破楼兰、戚殃、秦宴、虬（宇文戮虬）、仇千臣、仇于醪、曲适、任俊南、容予淮、容予清、三月三、商九曜、商昼、沈解尘、沈默、沈无寄、沈坐忘、宋麦冢、宿星河、苏辞镜、苏乞风、苏幸、姒、噬天、天笑、特克诺、荼甘华、陶任、万尽、王一乐、帷 那岐、温绮、闻人弈、闻昭、无明、夏新辰、弦七、向不讫、向逢意、萧不渡、萧行、萧珝、萧临、萧青山、萧芜、萧宴、萧昭、萧俊南、谢成念、谢桥、谢阳生、谢应清、辛未济、辛我归、许安之、薛定安、燕不还、晏长风、夜阑、叶孤舟、叶落悬、亦竟、殷秋来、殷无极、嬴却、应悬丝、游归鸿、余空草、虞妄、越华、越清乐、云琅、云清尘、赞恩、葬夭红、斩清和、展去冬、张成、张尚生、张天齐、章棺、赵若云、正北歌、栀爻、钟离愈、周北、周南、左丘彬、左丘妄、“静主”"
SP_RAW = "乐回、乐见、乐庭歌、乐月、陈秋月"

UR_CHARS = [x for x in UR_RAW.split("、") if x]
SSR_CHARS = [x for x in SSR_RAW.split("、") if x]
SP_CHARS = [x for x in SP_RAW.split("、") if x]

# 全角色同一个池子，抽卡时等概率
GACHA_POOL = (
    [("UR", n) for n in UR_CHARS]
    + [("SSR", n) for n in SSR_CHARS]
    + [("SP", n) for n in SP_CHARS]
)


# ================= 状态持久化与时间流逝 =================
STATE_FILE = os.environ.get("YUEYUE_STATE_FILE", "yueyue_state.json")
# 一个时间片等于多少分钟，默认 120。想快速验证可设环境变量 YUEYUE_TICK_MINUTES=1
TICK_MINUTES = float(os.environ.get("YUEYUE_TICK_MINUTES") or "120")
# 饱食和口渴都高于这个数，月月才有力气，精力恢复；否则精力下降
ENERGY_WELLFED_LINE = 30
# 离线太久最多补算多少个时间片，避免一开机就饿死
MAX_CATCHUP_TICKS = 24

_STATE_KEYS = ("hunger", "thirst", "energy", "affection")


def save_state():
    try:
        data = {k: bot_data[k] for k in _STATE_KEYS}
        data["last_tick"] = bot_data.get("last_tick")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存月月状态失败：", e)


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k in _STATE_KEYS:
            if k in saved:
                bot_data[k] = saved[k]
        bot_data["last_tick"] = saved.get("last_tick")
        print("已读取月月存档：", {k: bot_data[k] for k in _STATE_KEYS})
    except Exception as e:
        print("读取月月状态失败：", e)


def tick_once():
    """推进一个时间片：先结算精力，再扣饱食和口渴"""
    if bot_data["hunger"] > ENERGY_WELLFED_LINE and bot_data["thirst"] > ENERGY_WELLFED_LINE:
        bot_data["energy"] = min(100, bot_data["energy"] + 15)
    else:
        bot_data["energy"] = max(0, bot_data["energy"] - 20)
    bot_data["hunger"] = max(0, bot_data["hunger"] - 15)
    bot_data["thirst"] = max(0, bot_data["thirst"] - 15)


def catch_up():
    """按真实流逝的时间补算时间片，返回这次补了几片"""
    now = datetime.datetime.now(datetime.timezone.utc)
    steps = 1
    last = bot_data.get("last_tick")
    if last:
        try:
            prev = datetime.datetime.fromisoformat(last)
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=datetime.timezone.utc)
            elapsed = (now - prev).total_seconds()
            steps = max(1, int(elapsed // (TICK_MINUTES * 60)))
        except Exception:
            steps = 1
    steps = min(steps, MAX_CATCHUP_TICKS)
    for _ in range(steps):
        tick_once()
    bot_data["last_tick"] = now.isoformat()
    save_state()
    return steps


load_state()

# ================= 定时任务 =================
@tasks.loop(minutes=TICK_MINUTES)
async def decay_stats():
    try:
        n = catch_up()
        if n > 1:
            print(f"月月补算了 {n} 个时间片")
    except Exception as e:
        print("月月状态更新出错：", e)


@decay_stats.before_loop
async def _wait_before_decay():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def autosave():
    # 喂食、摸摸头之类的改动先留在内存，这里定期落盘兜底
    save_state()


@autosave.before_loop
async def _wait_before_autosave():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    if not decay_stats.is_running():
        decay_stats.start()
    if not autosave.is_running():
        autosave.start()
    try:
        synced = await bot.tree.sync()
        print(f"同步了 {len(synced)} 个斜杠命令")
    except Exception as e:
        print(f"同步失败：{e}")
    print("月月上线啦！名字是：", bot.user)

# ================= /回顶 =================
class TopButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="📍 回到顶部", style=discord.ButtonStyle.success, custom_id="go_to_top")
    async def go_top(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        try:
            first_msg = None
            async for msg in channel.history(limit=1, oldest_first=True):
                first_msg = msg
            if first_msg:
                link = f"https://discord.com/channels/{interaction.guild.id}/{channel.id}/{first_msg.id}"
                await interaction.response.send_message(f"{interaction.user.mention} 点下面的链接跳转到频道顶部👇\n{link}", ephemeral=True)
            else:
                await interaction.response.send_message(f"{interaction.user.mention} 这个频道好像没有消息呢～", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"{interaction.user.mention} 回顶失败，请稍后再试～", ephemeral=True)
            print(f"回顶报错：{e}")

@bot.tree.command(name="回顶", description="生成一个回顶按钮，点击跳转到频道顶部")
async def huidin(interaction: discord.Interaction):
    embed = discord.Embed(title="📍 回顶助手", description="点击下方按钮，我将帮你跳转到频道顶部～", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, view=TopButtonView())

# ================= 状态 =================
@bot.tree.command(name="状态", description="查看当前月月的状态和想法")
async def status(interaction: discord.Interaction):
    embed = discord.Embed(title="🦊🐙 月月的状态面板", color=discord.Color.purple())
    def bar(value):
        return "🟩" * (value // 10) + "⬛" * (10 - value // 10)
    embed.add_field(name="🍣 饱食度", value=f"{bar(bot_data['hunger'])} ({bot_data['hunger']}/100)", inline=False)
    embed.add_field(name="💧 口渴度", value=f"{bar(bot_data['thirst'])} ({bot_data['thirst']}/100)", inline=False)
    embed.add_field(name="⚡ 精力", value=f"{bar(bot_data['energy'])} ({bot_data['energy']}/100)", inline=False)
    embed.add_field(name="💕 全服亲密度", value=f"**{bot_data['affection']}** / 520", inline=False)
    _last = bot_data.get("last_tick")
    if _last:
        try:
            _prev = datetime.datetime.fromisoformat(_last)
            if _prev.tzinfo is None:
                _prev = _prev.replace(tzinfo=datetime.timezone.utc)
            _mins = int((datetime.datetime.now(datetime.timezone.utc) - _prev).total_seconds() // 60)
            embed.add_field(name="🕒 上次更新", value=f"{_mins} 分钟前", inline=False)
        except Exception:
            pass
    thought = random.choice(bot_data["thoughts"])
    embed.add_field(name="💭 月月现在在想什么", value=f"*{thought}...*", inline=False)
    await interaction.response.send_message(embed=embed)

# ================= 互动指令 =================
@bot.tree.command(name="摸摸头", description="摸摸月月的狐狸脑袋（每人每天限5次）")
async def pat(interaction: discord.Interaction):
    today = datetime.date.today().isoformat()
    user_record = pat_cooldown.get(interaction.user.id, {"date": today, "count": 0})
    if user_record["date"] != today:
        user_record = {"date": today, "count": 0}
    if user_record["count"] >= 5:
        await interaction.response.send_message("呜... 本座的脑袋要被摸秃了！明天再摸好不好嘛🥺", ephemeral=True)
        return
    user_record["count"] += 1
    pat_cooldown[interaction.user.id] = user_record
    bot_data["affection"] = min(520, bot_data["affection"] + 1)
    left_times = 5 - user_record["count"]
    pat_replies = [
        f"呜呜~ 好舒服，狐狸耳朵要化掉了... {interaction.user.mention} 再摸一下下嘛！🥺",
        f"汪！{interaction.user.mention} 本座的尾巴摇成螺旋桨啦！触手也不自觉地缠住你的手了哦！🐙💕",
        f"嘿嘿嘿... {interaction.user.mention} 的手心好温暖... 本座要蹭蹭！🦊✨",
        f"嗷呜！{interaction.user.mention} 本座的头可不是谁都能摸的！...但是你的话，可以多摸几下。"
    ]
    await interaction.response.send_message(random.choice(pat_replies) + f"\n*(全服亲密度 +1，当前 {bot_data['affection']}/520，你今天还能摸 {left_times} 次)*")

@bot.tree.command(name="喂食", description="给月月投喂好吃的或者喝的")
@app_commands.describe(food="你想喂月月吃什么或者喝什么？")
async def feed(interaction: discord.Interaction, food: str):
    food = food.strip()

    food_replies = {
    "小鱼干": f"汪汪汪！是小鱼干！{interaction.user.mention} 本座的触手全接住！🐙🐟",
    "三色丸子": f"嗷呜！是三色丸子！{interaction.user.mention} 本座最喜欢吃丸子了！🍡🦊",
    "鲷鱼烧": f"呜哇！是鲷鱼烧！{interaction.user.mention} 鱼鱼形状的！本座虽然是章鱼但本座不介意！🐟🍘",
    "金枪鱼罐头": f"汪！金枪鱼罐头！{interaction.user.mention} 本座的最爱！啊呜啊呜！🐙🥫",
    "猫薄荷饼干": f"嘿嘿，猫薄荷饼干！{interaction.user.mention} 本座虽然不是猫但吃了会晕乎乎的~🍪🦊",
    "章鱼烧": f"嗷呜！章鱼烧！{interaction.user.mention} 本座的同族！...但是太好吃了本座还是要吃！🐙🟠",
    "奶油泡芙": f"哇！奶油泡芙！{interaction.user.mention} 软乎乎的！触手都粘一起了！🧁🐙",
    "烤秋刀鱼": f"汪！烤秋刀鱼！{interaction.user.mention} 香喷喷！本座连骨头都嚼碎！🐟🔥",
    "草莓大福": f"呜哇！草莓大福！{interaction.user.mention} 酸酸甜甜的！狐狸耳朵竖起来了！🍓🦊",
    "鲜虾寿司": f"嗷呜！鲜虾寿司！{interaction.user.mention} 本座虽然是章鱼但也吃虾！🍤🐙",
    "鱼骨饼干": f"咔嚓咔嚓！鱼骨饼干！{interaction.user.mention} 脆脆的好好吃！🍪🦊",
    "小虾干": f"汪！小虾干！{interaction.user.mention} 本座一口一个！🦐🐙",
    "糙米鱼饼": f"嗷呜！糙米鱼饼！{interaction.user.mention} 健康又好吃！🍘🦊",
    "烤鱼串": f"呜哇！烤鱼串！{interaction.user.mention} 串起来的鱼！本座一串一串吃！🍢🐙",

    "榴莲": f"嗷呜！榴莲！{interaction.user.mention} 本座的三条触手都在抗议，但本座停不下来！🍈🦊",
    "三文鱼": f"汪！三文鱼！{interaction.user.mention} 高级货！本座要一小口一小口地吃！🍣🐙",
    "巧克力": f"呜哇！巧克力！{interaction.user.mention} 甜的！本座吃完整只狐狸都软了！🍫🦊",
    "咖啡": f"咕噜咕噜！咖啡！{interaction.user.mention} 本座喝完能通宵玩触手！☕🐙",
    "拿铁": f"吸溜~ 拿铁！{interaction.user.mention} 本座的触手都冒奶泡了！🥛🦊",
    "奶绿": f"嗷呜！奶绿！{interaction.user.mention} 茶香加奶香，本座要原地转三圈！🍵🐙",
    }

    # 喝的优先
    drinks = ["水", "饮料", "拿铁", "红茶", "咖啡", "奶茶", "可乐", "果汁", "茶"]
    is_drink = any(d in food for d in drinks)
    
    if is_drink:
        if bot_data["thirst"] >= 100:
            await interaction.response.send_message(f"汪！{interaction.user.mention} 本座喝不下了！🌊🐙")
            return
        bot_data["thirst"] = min(100, bot_data["thirst"] + 30)
        msg = ""
        if any(d in food for d in ["拿铁", "红茶", "咖啡", "可乐"]):
            bot_data["energy"] = min(100, bot_data["energy"] + 20)
            if food == "拿铁": msg = f"咕噜咕噜！是拿铁！{interaction.user.mention} 本座感觉精力充沛！☕🦊"
            elif food == "红茶": msg = f"吸溜~ 是红茶！{interaction.user.mention} 本座又能熬夜玩触手了！🍵🐙"
            elif food == "咖啡": msg = f"呜哇！是咖啡！{interaction.user.mention} 本座精神百倍！☕⚡"
            else: msg = f"咕噜咕噜！是快乐水！{interaction.user.mention} 本座充满活力！🥤🦊"
            msg += f"\n*(口渴度 +30，精力 +20)*"
        else:
            msg = f"咕噜咕噜！活过来了！{interaction.user.mention} 本座差点变成章鱼干！💧🐙\n*(口渴度 +30)*"
        await interaction.response.send_message(msg)
        return

    if bot_data["hunger"] >= 100:
        await interaction.response.send_message(f"汪！{interaction.user.mention} 本座吃不下了！肚子圆滚滚啦！🐙💦")
        return

    likes = ["小鱼干", "三色丸子", "鲷鱼烧", "金枪鱼罐头", "猫薄荷饼干", "章鱼烧", "奶油泡芙", "烤秋刀鱼", "草莓大福", "鲜虾寿司", "鱼骨饼干", "小虾干", "糙米鱼饼", "烤鱼串",
             "榴莲", "三文鱼", "巧克力", "咖啡", "拿铁", "奶绿"]
    dislikes = ["芹菜", "海带", "内脏", "辣", "辣条", "辣椒"]
    
    today = datetime.date.today().isoformat()
    feed_record = feed_cooldown.get(interaction.user.id, {"date": today, "count": 0})
    if feed_record["date"] != today:
        feed_record = {"date": today, "count": 0}
    
    if food in likes:
        bot_data["hunger"] = min(100, bot_data["hunger"] + 20)
        who = interaction.user.mention
        msg = food_replies.get(food, f"嗷呜！是{food}！{who} 本座吃得很开心！")
        if feed_record["count"] >= 1:
            feed_cooldown[interaction.user.id] = feed_record
            msg += "\n*(饱食度 +20，但你今天已经喂过啦，亲密度不再增加哦~)*"
            await interaction.response.send_message(msg)
            return
        feed_record["count"] += 1
        feed_cooldown[interaction.user.id] = feed_record
        bot_data["affection"] = min(520, bot_data["affection"] + 3)
        msg += f"\n*(饱食度 +20，亲密度 +3，当前 {bot_data['affection']}/520)*"
        await interaction.response.send_message(msg)
    elif any(d in food for d in dislikes):
        bot_data["affection"] = max(0, bot_data["affection"] - 5)
        await interaction.response.send_message(f"呜呜呜！{interaction.user.mention} 本座不要吃{food}！好难吃！💔\n*(亲密度 -5)*")
    else:
        bot_data["affection"] = max(0, bot_data["affection"] - 1)
        await interaction.response.send_message(f"诶？{food}？本座勉强吃一口... 呸呸呸！\n*(亲密度 -1)*")

@bot.tree.command(name="逗弄", description="随机逗弄月月，会发生什么呢？")
async def tease(interaction: discord.Interaction):
    change = random.randint(1, 5)
    is_positive = random.choice([True, False])
    if is_positive:
        bot_data["affection"] = min(520, bot_data["affection"] + change)
        replies = [
            f"嘿嘿，{interaction.user.mention} 你逗本座！本座偏不上当！🦊💕",
            f"汪！{interaction.user.mention} 看本座的触手反击！...哎呀自己缠住了！🐙💦",
            f"嗷呜~ {interaction.user.mention} 你真好玩！本座今天多粘你一会儿！"
        ]
        msg = random.choice(replies) + f"\n*(亲密度 +{change}，当前 {bot_data['affection']}/520)*"
    else:
        bot_data["affection"] = max(0, bot_data["affection"] - change)
        replies = [
            f"呜！{interaction.user.mention} 你欺负本座！💢",
            f"嗷呜... {interaction.user.mention} 本座不理你了！除非给三文鱼！😤",
            f"汪呜... {interaction.user.mention} 你居然揍本座... 触手都痛得缩起来了...😭"
        ]
        msg = random.choice(replies) + f"\n*(亲密度 -{change}，当前 {bot_data['affection']}/520)*"
    await interaction.response.send_message(msg)

@bot.tree.command(name="欺负", description="揍月月或者对月月做坏事……")
async def bully(interaction: discord.Interaction):
    decrease = random.randint(3, 5)
    bot_data["affection"] = max(0, bot_data["affection"] - decrease)
    replies = [
        f"呜哇！{interaction.user.mention} 你说本座弱智！本座明明只是神经大条！💔",
        f"汪呜... {interaction.user.mention} 你居然揍本座... 触手都痛得缩起来了...😭",
        f"嗷呜！！{interaction.user.mention} 本座要生气了！本座要把你零食全吃掉！哼！😡"
    ]
    await interaction.response.send_message(random.choice(replies) + f"\n*(亲密度 -{decrease}，当前 {bot_data['affection']}/520)*")

# ================= 算卦 =================
@bot.tree.command(name="算卦", description="让月月给你算一卦，看看今天的运气（纯属娱乐）")
async def fortune(interaction: discord.Interaction):
    idx, (name, comment) = random.choice(list(enumerate(HEXAGRAMS, 1)))
    embed = discord.Embed(title="🔮 月月算命摊", color=discord.Color.dark_purple())
    embed.add_field(name="你抽到的卦", value=f"第 {idx} 卦  {name}", inline=False)
    embed.add_field(name="月月说", value=comment, inline=False)
    embed.set_footer(text="纯属娱乐，别当真～命运还握在你自己手里，本座只是随口一说。")
    await interaction.response.send_message(embed=embed)


# ================= 抽卡 =================
@bot.tree.command(name="抽卡", description="看看今天你和哪位角色有缘分（纯属娱乐）")
async def gacha(interaction: discord.Interaction):
    rarity, name = random.choice(GACHA_POOL)
    embed = discord.Embed(title="🎴 今日缘分卡", color=discord.Color.gold())
    embed.add_field(name=f"{rarity}  {name}", value=f"恭喜 {interaction.user.mention}！", inline=False)
    embed.add_field(name="月月说", value="本座掐指一算～今天或许这个角色和你有缘分哦，要不要去打个招呼？", inline=False)
    embed.set_footer(text="纯属娱乐，抽到谁都是缘分～")
    await interaction.response.send_message(embed=embed)


# ================= 消息监听 =================
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content_raw = message.content.strip()
    if "投喂" in content_raw and "给月月" in content_raw and not message.author.bot:
        food_match = content_raw.replace("投喂", "").replace("给月月", "").strip()
        if food_match:
            feed_responses = [
                f"呜哇！{message.author.mention} 给本座带了{food_match}！超开心！🦊💕",
                f"汪！{message.author.mention} 专门投喂{food_match}！触手感动打结了！🐙✨",
                f"嗷呜~ {message.author.mention} 的{food_match}好好吃！🦊🐙"
            ]
            await message.channel.send(random.choice(feed_responses))
            await bot.process_commands(message)
            return

    if bot.user.mentioned_in(message):
        content = message.content.lower()
        author_id = message.author.id
        
        if "我要买奶茶" in content or "我要喝奶茶" in content:
            milk_tea_waiting[author_id] = True
            await message.channel.send(f"汪汪！{message.author.mention} 你要买奶茶吗？本座用触手给你摇！🧋🐙")
            await bot.process_commands(message)
            return
            
        if milk_tea_waiting.get(author_id) and "qqㄋㄟㄋㄟ好喝到咩噗茶" in content:
            del milk_tea_waiting[author_id]
            await message.channel.send(f"QQㄋㄟㄋㄟ好喝到咩噗茶？！{message.author.mention} 你居然真的点了！本座给你做！偷偷加一勺小鱼干！🐙🦊🧋")
            await bot.process_commands(message)
            return

        # 梗彩蛋优先
        for _keys, _replies in MEME_EGGS:
            if any(_k in content for _k in _keys):
                await message.channel.send(f"{message.author.mention} {random.choice(_replies)}")
                await bot.process_commands(message)
                return

        if bot_data["thirst"] < 30 and random.random() < 0.6:
            await message.channel.send(f"呜呜... {message.author.mention} 本座好渴... 能喝点水吗？🥺💧")
            await bot.process_commands(message)
            return
        if bot_data["hunger"] < 30 and random.random() < 0.6:
            await message.channel.send(f"呜呜... {message.author.mention} 本座肚子饿了... 喂点好吃的？🥺")
            await bot.process_commands(message)
            return
        if bot_data["energy"] < 20 and random.random() < 0.6:
            await message.channel.send(f"哈欠... {message.author.mention} 本座好困... 除非有咖啡... 😴☕")
            await bot.process_commands(message)
            return

        if any(word in content for word in ["亲亲", "抱抱", "举高高", "贴贴", "喜欢你", "爱你", "摸摸", "rua"]):
            bot_data["affection"] = min(520, bot_data["affection"] + 2)
            await message.channel.send(f"呜哇！{message.author.mention} 亲亲！狐狸耳朵红透了！🦊🐙💕 *(亲密度 +2)*")
            await bot.process_commands(message)
            return

        if any(word in content for word in ["弱智", "笨蛋", "傻子", "蠢", "丑", "滚", "讨厌"]):
            bot_data["affection"] = max(0, bot_data["affection"] - 5)
            await message.channel.send(f"呜哇！{message.author.mention} 你骂本座！本座去角落画圈圈！😭 *(亲密度 -5)*")
            await bot.process_commands(message)
            return

        # 萌新指路
        if "子区" in content or "右上角" in content:
            await message.channel.send(f"汪汪！{message.author.mention} 子区在**右上角**哦！🐙👉")
            await bot.process_commands(message)
            return
        elif "标注" in content:
            await message.channel.send(f"嗷呜~ {message.author.mention} 发图时长按图片添加标注！🦊✨")
            await bot.process_commands(message)
            return
        elif "频道" in content or "在哪" in content:
            await message.channel.send(f"呜哇！{message.author.mention} 频道在左侧列表！🐙💦")
            await bot.process_commands(message)
            return
        elif "发图" in content or "发照片" in content:
            await message.channel.send(f"汪！{message.author.mention} 拖图片进来或点➕号！🦊📸")
            await bot.process_commands(message)
            return
        elif "@" in content or "艾特" in content or "at人" in content:
            await message.channel.send(f"嗷呜~ {message.author.mention} 打 @ 选名字！🐙")
            await bot.process_commands(message)
            return
        elif "审核" in content:
            await message.channel.send(f"萌新宝宝！{message.author.mention} 去 **#在这里审核** 看看！🦊🔐")
            await bot.process_commands(message)
            return
        elif "规则" in content:
            await message.channel.send(f"汪汪！{message.author.mention} 先看 **#欢迎-和-规则**！🐙😭")
            await bot.process_commands(message)
            return
        elif "萌新" in content or "不会玩" in content or "新人" in content:
            await message.channel.send(f"诶？{message.author.mention} 不会玩？问 **#💬聊聊**！本座罩着你！🦊💕")
            await bot.process_commands(message)
            return

        # 酒馆指路
        elif "单人卡" in content:
            await message.channel.send(f"汪汪！{message.author.mention} 单人卡去 **#单人卡**！🦊🃏")
            await bot.process_commands(message)
            return
        elif "群像卡" in content or "群像" in content:
            await message.channel.send(f"嗷呜~ {message.author.mention} 群像卡去 **#群像卡**！🐙✨")
            await bot.process_commands(message)
            return
        elif "章鱼特供" in content:
            await message.channel.send(f"汪！{message.author.mention} 章鱼特供在 **#章鱼特供**！🦊🐙")
            await bot.process_commands(message)
            return
        elif "美化" in content or "组件" in content:
            await message.channel.send(f"嗷呜~ {message.author.mention} 去 **#美化和小组件**！🐙✨")
            await bot.process_commands(message)
            return
        elif "点菜" in content:
            await message.channel.send(f"汪！{message.author.mention} 去 **#📝点菜**！🦊🍽️")
            await bot.process_commands(message)
            return
        elif "番外" in content:
            await message.channel.send(f"嗷呜！{message.author.mention} 番外去 **#吃点番外**！🐙📖")
            await bot.process_commands(message)
            return
        elif "mj" in content or "小铺" in content:
            await message.channel.send(f"汪！{message.author.mention} mj去 **#章鱼的mj小铺**！🦊🛍️")
            await bot.process_commands(message)
            return
        elif "bug" in content or "报错" in content:
            await message.channel.send(f"呜哇！{message.author.mention} bug看 **#教学-bug看这里**！🐙🔧")
            await bot.process_commands(message)
            return
        elif "绘图" in content or "画画" in content:
            await message.channel.send(f"嗷呜！{message.author.mention} 绘图看 **#绘图区规则**！🦊🎨")
            await bot.process_commands(message)
            return
        elif "语音" in content or "音色" in content:
            await message.channel.send(f"汪汪！{message.author.mention} 语音去 **#AI语音区**！🐙🎤")
            await bot.process_commands(message)
            return
        elif "金主" in content:
            await message.channel.send(f"嘿嘿，{message.author.mention} 金主去 **#感谢金主**！🐙💖")
            await bot.process_commands(message)
            return
        elif "摸鱼" in content:
            await message.channel.send(f"汪！{message.author.mention} 本座在 **#摸鱼中**！🦊🐟")
            await bot.process_commands(message)
            return
        elif "拼车" in content or "nai" in content:
            await message.channel.send(f"汪！{message.author.mention} 拼车去 **#nai拼车频道**！🐙🚗")
            await bot.process_commands(message)
            return

        normal_replies = [
            f"汪汪！{message.author.mention} 你叫我嘛？贴贴！🦊🐙",
            f"嗷呜~ {message.author.mention}！本座在呢！今天也要乖乖的哦！",
            f"{message.author.mention} 嘿嘿，想本座啦？本座刚才玩触手没听到！",
            f"汪！{message.author.mention} 本座听到啦！需要帮忙吗？",
            f"{message.author.mention} 抱抱！触手超软的哦！🦊🐙"
        ]
        await message.channel.send(random.choice(normal_replies))

    await bot.process_commands(message)

bot.run(os.getenv("DISCORD_TOKEN"))