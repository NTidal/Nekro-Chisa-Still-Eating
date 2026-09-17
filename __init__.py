"""千小妹还在吃 (Chisa Still Eating) - NekroAgent 移植版

跨次元干饭摇号插件。移植自 AstrBot 插件 astrbot_plugin_chisa_still_eating v4.2.4
（原作者 Rua432，MIT 协议）。

功能：
- 吃什么/喝什么/来点黑暗料理：多世界权重摇号、群内防重历史、厨师羁绊召唤、干饭人截胡
- 群内加菜 / 上传厨师（带图）、黑/白名单、管理员白名单
- 千小妹商会：60 秒交互菜单、目录同步、进货/招募/黑魔法召唤、99.2MB 基础图库一键拉取
- WebUI 管理大屏（在 NekroAgent 插件页打开）：图库/干饭人管理、网页版商会进货、皮肤工坊、
  吉祥物随机池与自定义文案
- 多镜像测速 + 逐跳 HTTPS 白名单 + SHA-256 校验 + 安全解压

未实装：OneBot 合并转发（NekroAgent 段消息无对应原语，长消息以普通文本发送）；
AI 拟人播报（原版依赖 AstrBot 宿主 LLM 回调机制，NA 版使用内置文案模板）。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import tempfile
import time
from pathlib import Path
from typing import List, Optional

import httpx
from pydantic import Field

from nekro_agent.api.core import logger
from nekro_agent.api.plugin import ConfigBase, NekroPlugin
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.schemas.agent_message import AgentMessageSegment, AgentMessageSegmentType
from nekro_agent.schemas.chat_message import ChatMessage, ChatMessageSegmentType
from nekro_agent.schemas.signal import MsgSignal
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.tools.common_util import copy_to_upload_dir
from nekro_agent.tools.path_convertor import convert_filename_to_sandbox_upload_path

from . import shop
from .core import (
    FoodDataManager,
    ImageManager,
    RateLimiter,
    load_templates,
    load_worlds,
    rebuild_alias_map,
    resolve_active_key,
    scan_ganfanren,
)

plugin = NekroPlugin(
    name="千小妹还在吃",
    module_name="chisa_eating",
    author="NTidal",
    version="4.2.4-na",
    description="跨次元干饭摇号插件：吃什么/喝什么盲盒、厨师羁绊召唤、干饭人截胡、千小妹商会云端进货、WebUI 图库管理与皮肤工坊",
    url="https://github.com/NTidal/Nekro-Chisa-Still-Eating",
    support_adapter=["onebot_v11", "sse", "discord", "qqbot_openclaw"],
    sleep_brief="用户纠结吃什么/喝什么、要玩干饭摇号、或要管理千小妹图库/商会进货时激活。",
    webui_path="webui.html",
)


@plugin.mount_config()
class ChisaConfig(ConfigBase):
    """千小妹还在吃配置"""

    # ---- 权限 ----
    ADMIN_USERS: List[str] = Field(
        default=[],
        title="管理员账号白名单",
        description="可使用商会进货/加菜/上传厨师/更新图库/重载等管理指令的用户平台ID（如 QQ 号）列表，每行一个。"
                    "留空则所有人都无法使用管理类指令（普通摇号玩法不受影响）。WebUI 不经过聊天平台，无此限制。",
    )

    # ---- 触发词（子串匹配，命中即直连摇号）----
    TRIGGER_EAT: List[str] = Field(
        default=["吃什么", "吃啥", "吃点儿啥"], title="吃什么触发词",
        description="消息包含其中任意词即触发全宇宙随机美食。每行一个，可自行增删。")
    TRIGGER_DRINK: List[str] = Field(
        default=["喝什么", "喝啥", "喝点儿啥"], title="喝什么触发词")
    TRIGGER_DARK: List[str] = Field(
        default=["来点黑暗料理"], title="黑暗料理触发词",
        description="整活玩法：抽到奇怪的菜名+黑暗配图。")
    TRIGGER_COMMON_EAT: List[str] = Field(
        default=["来点现实的食物", "来点三次元食物"], title="现实食物触发词",
        description="只从三次元/现实卡池抽（外卖风）。")
    TRIGGER_COMMON_DRINK: List[str] = Field(
        default=["来点现实的饮品", "来点三次元饮品"], title="现实饮品触发词")

    # ---- 卡池权重（相对权重，无需加起来等于 100）----
    WEIGHT_COMMON: int = Field(
        default=70, title="三次元/现实 权重", ge=0,
        description="各卡池为相对权重，按比例命中，无需总和为 100。默认 70。")
    WEIGHT_W1: int = Field(default=20, title="世界1（鸣潮）权重", ge=0)
    WEIGHT_W2: int = Field(default=5, title="世界2（原神）权重", ge=0)
    WEIGHT_W3: int = Field(default=5, title="世界3（终末地）权重", ge=0)
    WEIGHT_W4: int = Field(default=0, title="世界4（自定义）权重", ge=0,
                           description="世界4 默认无图库，需自行加菜后再调大权重。")

    # ---- 摇号行为 ----
    HISTORY_LIMIT: int = Field(
        default=30, title="防重历史长度", ge=0,
        description="每个聊天记住最近多少道菜不重复抽中，0 关闭防重记忆。默认 30。")
    SPAM_THRESHOLD: int = Field(
        default=3, title="防刷屏阈值", ge=1,
        description="60 秒内点菜次数超过该值即触发导游吐槽拦截。默认 3。")
    EGG_PROB: int = Field(
        default=10, title="干饭人截胡概率(%)", ge=0, le=100,
        description="摇号结果被已招募干饭人抢走的概率（整活）。默认 10。")
    CHEF_MEME_PROB: int = Field(
        default=50, title="厨师立绘概率(%)", ge=0, le=100,
        description="召唤厨师/羁绊料理时附带厨师立绘的概率。默认 50。")
    GLOBAL_MEME_PROB: int = Field(
        default=30, title="导游表情包概率(%)", ge=0, le=100,
        description="摇号时附带主活动世界导游情绪表情包的概率。默认 30。")
    INTERCEPTION_EGG_CHANCE: int = Field(
        default=50, title="刷屏拦截时截胡概率(%)", ge=0, le=100,
        description="被防刷屏拦截时，干饭人出来抢饭的概率。默认 50。")
    REPEAT_PROB: int = Field(
        default=10, title="复读摆烂概率(%)", ge=0, le=100,
        description="导游学用户说话/摆烂复读的概率。默认 10。")
    REPEAT_COOLDOWN: int = Field(
        default=60, title="复读冷却(秒)", ge=0,
        description="同一聊天两次复读之间的最短间隔。默认 60。")
    EGG_POOL: str = Field(
        default="", title="指定干饭人卡池",
        description="留空=全部已招募干饭人随机；只让部分干饭人出场时填名字，多个用分号(;)分隔。")
    ACTIVE_WORLD: str = Field(
        default="世界1(鸣潮)", title="主活动世界",
        description="世界1(鸣潮)/世界2(原神)/世界3(终末地)/世界4。决定导游自称、情绪表情包与「忠诚模式」卡池。")
    CONVERT_MEME_TO_GIF: bool = Field(
        default=True, title="配菜表情包转 GIF",
        description="将随餐发送的表情包（导游/干饭人）转为 GIF 发送，QQ 下会渲染为小表情气泡。"
                    "需 Pillow，缺失或失败时自动回退原图。")
    MODE_LOYAL: bool = Field(default=False, title="忠诚模式", description="只抽主活动世界 + 三次元卡池。")
    MODE_ROLLER: bool = Field(default=False, title="摇号机模式", description="排除三次元卡池（只抽二次元世界）。")
    MODE_NORMIE: bool = Field(default=False, title="现充模式", description="只抽三次元/现实卡池。")

    # ---- 聊天黑白名单 ----
    ENABLE_BLACKLIST: bool = Field(default=False, title="启用群黑名单")
    BLACKLIST_CHATS: List[str] = Field(
        default=[], title="黑名单聊天",
        description="填群号或 chat_key，每行一个，命中的聊天插件完全不响应。")
    ENABLE_WHITELIST: bool = Field(default=False, title="启用群白名单")
    WHITELIST_CHATS: List[str] = Field(
        default=[], title="白名单聊天",
        description="填群号或 chat_key，每行一个，仅命中的聊天响应（私聊不受影响时建议同时测试）。")

    # ---- 直连触发灵敏度 ----
    EXACT_TRIGGER_ONLY: bool = Field(
        default=False,
        title="仅精确/短消息触发摇号",
        description="开启后，吃什么/喝什么/特产/厨师召唤等摇号玩法只在消息很短"
                    "（去除标点、@、英文数字后的长度 ≤ 下方「摇号触发最大字数」）时直连回复；"
                    "更长的自然口语（如「我想问问今天吃什么比较好呢」）放行给主 Agent 处理，避免抢话。"
                    "商会/加菜/上传厨师/帮助等管理指令本就是精确匹配，不受此开关影响。"
                    "默认关闭=子串命中即直连。",
    )
    TRIGGER_MAX_LEN: int = Field(
        default=10, title="摇号触发最大字数", ge=1,
        description="仅在「仅精确/短消息触发」开启时生效。消息归一化后的字符数不超过该值才直连摇号。"
                    "默认 10（中文按字符计）。")

    # ---- WebUI 吉祥物 ----
    MASCOT_CLICK_SWITCH: bool = Field(
        default=True, title="点击吉祥物随机切换",
        description="WebUI 右下角吉祥物：数据目录 mascots/ 文件夹放入多张图片后，"
                    "点击吉祥物会随机切换图片并弹出随机文案（文案在 WebUI「🐾 吉祥物」面板编辑，"
                    "存于 mascot_quotes.txt）。关闭后点击只弹文案不换图。文件夹为空时显示内置千小妹。")


config: ChisaConfig = plugin.get_config(ChisaConfig)

# ============================================================
# 运行时状态
# ============================================================

_shop_sessions: dict[str, float] = {}


class _State:
    def __init__(self):
        self.data_dir: Path = plugin.get_plugin_data_dir()
        self.resource_dir: Path = Path(plugin._get_source_dir()) / "resource"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.wv_settings: dict = {}
        self.templates: dict = {}
        self.cfg_dict: dict = {}
        self.image_mgr = ImageManager(self.data_dir)
        self.limiter = RateLimiter()
        self.food_mgr: Optional[FoodDataManager] = None
        self.ganfanren: dict = {}
        self.alias_map: dict = {}
        self.reload()

    def reload(self):
        self.wv_settings = load_worlds(self.data_dir, self.resource_dir)
        self.templates = load_templates(self.data_dir, self.resource_dir)
        self.cfg_dict = self._build_cfg_dict()
        self.image_mgr.reload_caches(self.wv_settings)
        self.ganfanren = scan_ganfanren(self.data_dir)
        self.alias_map = rebuild_alias_map(self.wv_settings)
        if self.food_mgr is None:
            self.food_mgr = FoodDataManager(self.data_dir, self.cfg_dict)
        else:
            self.food_mgr.config = self.cfg_dict

    def _build_cfg_dict(self) -> dict:
        c = config
        d = {
            "history_limit": c.HISTORY_LIMIT,
            "spam_threshold": c.SPAM_THRESHOLD,
            "weight_3d": c.WEIGHT_COMMON,
            "weight_w1": c.WEIGHT_W1,
            "weight_w2": c.WEIGHT_W2,
            "weight_w3": c.WEIGHT_W3,
            "weight_w4": c.WEIGHT_W4,
            "weight_w5": 0,
            "mode_loyal": c.MODE_LOYAL,
            "mode_roller": c.MODE_ROLLER,
            "mode_normie": c.MODE_NORMIE,
            "egg_prob": c.EGG_PROB,
            "chef_meme_prob": c.CHEF_MEME_PROB,
            "global_meme_prob": c.GLOBAL_MEME_PROB,
            "interception_egg_chance": c.INTERCEPTION_EGG_CHANCE,
            "repeat_prob": c.REPEAT_PROB,
            "repeat_cooldown": c.REPEAT_COOLDOWN,
            "egg_pool": c.EGG_POOL,
            "convert_meme_to_gif": c.CONVERT_MEME_TO_GIF,
            "active_world": c.ACTIVE_WORLD,
            "common_food_text": [],
            "common_drink_text": [],
        }
        d.update(self.templates)
        return d


_state: Optional[_State] = None


def _st() -> _State:
    global _state
    if _state is None:
        _state = _State()
    return _state


# ============================================================
# 消息发送辅助
# ============================================================


async def _make_gif_copy(path: str) -> str:
    """将静态图转为 GIF 临时文件（QQ 下渲染为小表情气泡）；无 Pillow 或失败时回退原图。"""
    if not path or not os.path.exists(path) or path.lower().endswith(".gif"):
        return path
    try:
        from PIL import Image

        temp_path = os.path.join(tempfile.gettempdir(), f"chisa_meme_{os.getpid()}_{random.randint(100000, 999999)}.gif")

        def _convert():
            with Image.open(path) as img:
                if img.mode not in ("RGB", "RGBA", "P"):
                    img = img.convert("RGBA")
                img.save(temp_path, format="GIF")

        await asyncio.get_event_loop().run_in_executor(None, _convert)
        return temp_path
    except Exception as e:
        logger.debug(f"[千小妹] GIF 转换失败，回退原图: {e}")
        return path


async def _send_reply(_ctx: AgentCtx, text: str, images: Optional[List[str]] = None):
    """发送 文本+图片(可多张) 合并为一条消息。图片为本地路径，先复制到 uploads 再转沙盒路径。"""
    segments = [AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=text)]
    for img in images or []:
        if not img or not os.path.exists(img):
            continue
        p = Path(img)
        try:
            host_path, _ = await copy_to_upload_dir(str(p), p.name, from_chat_key=_ctx.chat_key)
            sandbox_path = str(convert_filename_to_sandbox_upload_path(Path(host_path)))
            segments.append(AgentMessageSegment(type=AgentMessageSegmentType.IMAGE, content=sandbox_path))
        except Exception as e:
            logger.warning(f"[千小妹] 图片发送失败 {img}: {e}")
    try:
        adapter = await adapter_utils.get_adapter_for_ctx(_ctx)
        await universal_chat_service.send_agent_message(_ctx.chat_key, segments, adapter, _ctx, record=False)
    except Exception as e:
        logger.warning(f"[千小妹] 段消息发送失败，回退纯文本: {e}")
        try:
            await _ctx.send_text(text, record=False)
        except Exception:
            pass


async def _send_text(_ctx: AgentCtx, text: str):
    try:
        await _ctx.send_text(text, record=False)
    except Exception as e:
        logger.warning(f"[千小妹] 文本发送失败: {e}")


# ============================================================
# 工具
# ============================================================


def _is_admin(message: ChatMessage) -> bool:
    admins = {str(x).strip() for x in config.ADMIN_USERS if str(x).strip()}
    if not admins:
        return False
    uid = str(getattr(message, "platform_userid", "") or message.sender_id or "")
    return uid in admins


def _chat_allowed(chat_key: str) -> bool:
    def _hit(values: List[str]) -> bool:
        return any(v and (v == chat_key or v in chat_key) for v in values)

    if config.ENABLE_BLACKLIST and _hit(config.BLACKLIST_CHATS):
        return False
    if config.ENABLE_WHITELIST and not _hit(config.WHITELIST_CHATS):
        return False
    return True


def _extract_image_refs(message: ChatMessage) -> List[str]:
    """提取消息中的图片引用（URL 或本地路径）。"""
    refs = []
    for seg in getattr(message, "content_data", []) or []:
        seg_type = getattr(seg, "type", None)
        if seg_type != ChatMessageSegmentType.IMAGE and str(seg_type).lower() != "image":
            continue
        content = getattr(seg, "content", None)
        if isinstance(content, dict):
            content = content.get("url") or content.get("file") or content.get("path")
        if not content:
            content = getattr(seg, "url", None)
        if content:
            refs.append(str(content))
    return refs


async def _fetch_image_bytes(ref: str) -> Optional[tuple[bytes, str]]:
    """下载/读取图片，返回 (bytes, 扩展名)。"""
    if ref.startswith("http://") or ref.startswith("https://"):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(ref)
                if resp.status_code == 200:
                    ext = ".jpg"
                    low = ref.lower()
                    if ".png" in low:
                        ext = ".png"
                    elif ".gif" in low:
                        ext = ".gif"
                    elif ".webp" in low:
                        ext = ".webp"
                    return resp.content, ext
        except Exception as e:
            logger.warning(f"[千小妹] 图片下载失败 {ref}: {e}")
        return None
    # 本地路径
    p = Path(ref)
    if p.exists():
        return p.read_bytes(), p.suffix if p.suffix else ".jpg"
    return None


def _world_alias(st: _State, world_input: str) -> Optional[str]:
    if world_input in ("三次元", "现实", "common"):
        return "common"
    if world_input in st.alias_map:
        return st.alias_map[world_input]
    for alias, w_key in st.alias_map.items():
        if world_input == alias or world_input == w_key:
            return w_key
    return None


# ============================================================
# 帮助文案
# ============================================================

HELP_TEXT = """🌸 【千小妹跨次元干饭指南】 🌸
不知道今天吃啥？让异次元的导游们为你随机摇号吧！

🎲 基础盲盒（全宇宙随机）
💬 吃什么 / 吃啥：全宇宙卡池随机抽选一道美食。
💬 喝什么 / 喝啥：全宇宙卡池随机抽选一杯饮品。

🌍 定向打卡
💬 来点现实的食物 / 来点现实的饮品：只想吃地球上的普通外卖？用这个！
💬 [别名]特产 / [别名]特饮：精准锁定某个世界（例如：鸣潮特产、原神特饮）。

👑 羁绊召唤
💬 召唤[厨师名]下厨 / [厨师名]特供料理：只吃TA亲手做的菜，附赠专属立绘！
（例如：召唤爱弥斯下厨、弗洛洛特供料理）

☠️ 娱乐整活
💬 来点黑暗料理：导游的恶作剧，吃出人命概不负责！
💡 频繁点菜会被导游吐槽，饭还可能被干饭人"截胡"抢走哦！
---
🛒 千小妹商会（管理员）
💬 千小妹商会：唤出云端商会菜单，回复数字极速进货！
💬 进货[编号] / 招募[编号] / 黑魔法召唤[编号]：一键下载官方商品。
💬 千小妹商会信息同步：强制拉取最新商品名录。
---
⚙️ 管理指令（需在插件配置填管理员白名单）
💬 更新千小妹图库：拉取 99.2MB 基础图库（镜像加速+SHA256校验）。
💬 千小妹图库下载进度：查看后台下载进度。
💬 加菜 [世界] [分类] [菜名]（带图，空格分隔）
   例：加菜 三次元 食物 肯德基肉霸堡（配图）
💬 上传厨师 [厨师名]（带图）例：上传厨师 刻晴（配图）
💬 千小妹重载：手动放入图片/改好 worlds.json 后刷新缓存。

🖥️ 图形化管理：NekroAgent 插件页打开「千小妹还在吃」WebUI，
   可可视化管理图库/干饭人、网页版商会进货、订阅皮肤工坊换主题。

📁 图库目录：插件数据目录下的 food/ drink/ darkfood/ chefs/ memes/ ganfanren/
（原项目：github.com/dddada123/astrbot_plugin_chisa_still_eating ，原作者 Rua432）"""

QUICK_TEXT = """📌 【千小妹速查表】

🍔 基础功能
· 吃什么 / 喝点啥
· 来点现实的食物 / 鸣潮特产

👑 进阶与整活
· 来点黑暗料理
· 召唤[某人]下厨 / [某人]特供料理

🛒 商会系统（管理员）
· 千小妹商会
· 进货[编号] / 招募[编号] / 黑魔法召唤[编号]
· 千小妹商会信息同步

⚙️ 管理指令（管理员）
· 更新千小妹图库
· 千小妹图库下载进度
· 加菜 [世界] [分类] [菜名]（带图）
· 上传厨师 [厨师名]（带图）
· 千小妹重载"""

EMPTY_LIB_TEXT = """【千小妹系统提示】检测到基础图库为空！

🛠️ 【全自动拉取】：
请先在插件配置「管理员账号白名单」填入你的平台ID，
然后发送 更新千小妹图库 即可触发 99.2MB 基础图库的安全拉取
（国内镜像加速 + SHA-256 防投毒校验，完成后自动解压入库）。

📦 【手动兜底】（若拉取失败）：
夸克：https://pan.quark.cn/s/301110d45a48
百度：https://pan.baidu.com/s/1ZHfYz8vNL5JU0jyFtHiYqQ?pwd=erm9
解压后把 food、drink、chefs 等文件夹放入本插件数据目录即可。"""


# ============================================================
# 消息入口
# ============================================================


@plugin.mount_on_user_message()
async def on_user_message(_ctx: AgentCtx, message: ChatMessage) -> Optional[MsgSignal]:
    text = (message.content_text or "").strip()
    if not text:
        return None
    if not _chat_allowed(message.chat_key):
        return None

    st = _st()
    uid = str(getattr(message, "platform_userid", "") or message.sender_id or "")
    session_key = f"{message.chat_key}:{uid}"

    # ---------- 商会 60 秒交互会话 ----------
    if session_key in _shop_sessions:
        if time.time() - _shop_sessions[session_key] > 60:
            _shop_sessions.pop(session_key, None)
        else:
            choice = text.strip()
            if choice in ("1", "2", "3"):
                _shop_sessions.pop(session_key, None)
                catalog = shop.read_catalog(st.data_dir)
                if not catalog:
                    await _send_text(_ctx, "⚠️ 无法读取目录数据，请重新同步。")
                    return MsgSignal.BLOCK_TRIGGER
                mapping = {
                    "1": (["gf", "cf", "gd"], "千小妹商会 - 招募通道", "招募{id}"),
                    "2": (["fd", "dr"], "千小妹商会 - 餐饮通道", "进货{id}"),
                    "3": (["dk"], "千小妹商会 - 次元裂缝", "黑魔法召唤{id}"),
                }
                cats, title, cmd_format = mapping[choice]
                results: dict[str, list] = {}
                for item in catalog:
                    cat = item.get("cat", "")
                    if cat in cats:
                        results.setdefault(cat, []).append(item)
                emoji_map = {
                    "fd": "🍔 食品区", "dr": "🧋 饮品区",
                    "gf": "🏃 干饭人", "cf": "👨‍🍳 大厨", "gd": "🌸 导游MEME",
                    "dk": "☠️ 黑暗料理",
                }
                msg = f"📦 {title} 📦\n回复 \"{cmd_format.format(id='[编号]')}\" 即可下载对应包体\n\n"
                for cat_key in cats:
                    msg += f"{emoji_map.get(cat_key, cat_key)}\n"
                    items = results.get(cat_key, [])
                    if items:
                        for item in items:
                            msg += f"·[{item.get('id', '')}] {item.get('title', '')}\n"
                    else:
                        msg += "·这个分类暂时还没有商品上架·\n"
                    msg += "\n"
                await _send_text(_ctx, msg.strip())
                return MsgSignal.BLOCK_TRIGGER
            elif len(choice) <= 2:
                _shop_sessions[session_key] = time.time()
                await _send_text(_ctx, "输入错误哦，请回复 1、2 或 3")
                return MsgSignal.BLOCK_TRIGGER
            else:
                _shop_sessions.pop(session_key, None)

    # ---------- 帮助 / 速查 ----------
    if text in ("千小妹还在吃帮助", "千咲吃什么帮助", "干饭帮助", "美食帮助", "千小妹帮助", "千小妹吃什么帮助"):
        await _send_text(_ctx, HELP_TEXT)
        return MsgSignal.BLOCK_TRIGGER
    if text in ("千小妹速查", "/千小妹速查"):
        await _send_text(_ctx, QUICK_TEXT)
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 商会菜单 ----------
    if text == "千小妹商会":
        if not _is_admin(message):
            await _send_text(_ctx, "哼！千小妹商会重地，闲人免进！只有签了契约的管理员才能进去进货哦~")
            return MsgSignal.BLOCK_TRIGGER
        if not shop.catalog_path(st.data_dir).exists():
            await _send_text(_ctx, "仓库空空如也！是否进行千小妹商会信息同步？\n（请回复：千小妹商会信息同步）")
            return MsgSignal.BLOCK_TRIGGER
        _shop_sessions[session_key] = time.time()
        await _send_text(
            _ctx,
            "🎀 千小妹商会营业中 🎀\n欢迎老板！请在 60 秒内回复数字选择进货通道：\n"
            "1️⃣ 干饭人/大厨/导游招募\n2️⃣ 云食品/云饮品仓库\n3️⃣ 黑暗料理次元裂缝",
        )
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 商会信息同步 ----------
    if text in ("/千小妹商会信息同步", "千小妹商会信息同步拉取Json", "千小妹商会信息同步"):
        if not _is_admin(message):
            await _send_text(_ctx, "只有管理员可以同步商会信息哦！")
            return MsgSignal.BLOCK_TRIGGER
        await _send_text(_ctx, "正在联系商会总仓...请稍等片刻哦~")
        result = await shop.sync_catalog(st.data_dir)
        await _send_text(_ctx, result)
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 进货 / 招募 / 黑魔法召唤 ----------
    dl_match = re.match(r"^(进货|招募|黑魔法召唤)\[?([a-z]{2}\d{4})\]?$", text, re.IGNORECASE)
    if dl_match:
        if not _is_admin(message):
            await _send_text(_ctx, "只有管理员才能操作商会进货哦！")
            return MsgSignal.BLOCK_TRIGGER
        dlc_id = dl_match.group(2).lower()
        if shop.download_state["is_downloading"]:
            pct = int(shop.download_state["downloaded_bytes"] / max(shop.download_state["total_bytes"], 1) * 100)
            await _send_text(_ctx, f"📦 千小妹正在狂奔搬运中... 进度 [{pct}%]，请等待当前进货完成后再操作哦~")
            return MsgSignal.BLOCK_TRIGGER
        catalog = shop.read_catalog(st.data_dir)
        if not catalog:
            await _send_text(_ctx, "⚠️ 无法读取目录数据，请先发送 千小妹商会 进行同步。")
            return MsgSignal.BLOCK_TRIGGER
        target_item = next((i for i in catalog if i.get("id", "").lower() == dlc_id), None)
        if not target_item:
            await _send_text(_ctx, f"找不到编号为 {dlc_id} 的商品呢，老板是不是记错啦？")
            return MsgSignal.BLOCK_TRIGGER
        sha256 = target_item.get("sha256", "")
        await _send_text(_ctx, f"收到！千小妹这就去进货 {dlc_id}，请稍等片刻...")
        asyncio.create_task(_dlc_download_task(_ctx, dlc_id, sha256, st))
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 下载进度 ----------
    if text in ("/千小妹图库下载进度", "千小妹图库下载进度"):
        if shop.download_state["is_downloading"]:
            mb = shop.download_state["downloaded_bytes"] / (1024 * 1024)
            await _send_text(_ctx, f"【千小妹下载进度】\n正在为您搬运跨次元美食资源...\n当前已下载: {mb:.2f} MB")
        else:
            await _send_text(_ctx, "【千小妹提示】当前没有正在进行的图库下载任务哦。")
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 更新基础图库 ----------
    if "更新千小妹图库" in text:
        if not _is_admin(message):
            await _send_text(_ctx, "【权限不足】只有管理员才能执行图库更新指令哦！")
            return MsgSignal.BLOCK_TRIGGER
        if shop.download_state["is_downloading"]:
            await _send_text(_ctx, "【千小妹提示】图库正在下载中，请勿重复触发...")
            return MsgSignal.BLOCK_TRIGGER
        asyncio.create_task(_assets_download_task(_ctx, st))
        await _send_text(
            _ctx,
            "【千小妹提示】已收到！开始从镜像/Github 拉取基础图库 (约99.2MB)，"
            "发送 千小妹图库下载进度 可查看进度，完成后会自动通知~",
        )
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 重载缓存 ----------
    if text in ("千小妹重载", "/千小妹重载"):
        if not _is_admin(message):
            await _send_text(_ctx, "只有管理员才能重载千小妹后厨哦！")
            return MsgSignal.BLOCK_TRIGGER
        st.reload()
        names = "、".join(st.ganfanren.keys()) or "（暂无）"
        await _send_text(_ctx, f"✅ 千小妹后厨已刷新！图库/干饭人/世界配置全部重载。\n当前干饭人：{names}")
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 加菜 / 上传厨师 ----------
    if "加菜 " in text or "/加菜 " in text:
        await _handle_add_food(_ctx, message, text, st)
        return MsgSignal.BLOCK_TRIGGER
    if "上传厨师" in text or "/上传厨师" in text:
        await _handle_upload_chef(_ctx, message, text, st)
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 下载中拦截插件相关指令 ----------
    is_plugin_cmd = any(
        k in text
        for k in ("进货", "招募", "黑魔法召唤", "帮助", "吃什么", "喝什么", "特产", "特饮",
                  "吃饭", "料理", "召唤", "特供", "加菜", "上传厨师")
    )
    if shop.download_state["is_downloading"] and is_plugin_cmd:
        mb = shop.download_state["downloaded_bytes"] / (1024 * 1024)
        await _send_text(_ctx, f"【千小妹基础图库下载进度】\n下载完成后即可正常使用~\n当前已下载: {mb:.2f} MB / 99.20 MB")
        return MsgSignal.BLOCK_TRIGGER

    # ---------- 摇号玩法 ----------
    # 仅精确/短消息触发：去除标点、空白、@ 后，消息过长则视为自然口语，放行给主 Agent
    if config.EXACT_TRIGGER_ONLY:
        norm = re.sub(r"[\s，。？！、~…·\.,\!\?：:；;「」『』“”\"'（）()@＠a-zA-Z0-9_]+", "", text)
        if len(norm) > max(0, int(config.TRIGGER_MAX_LEN)):
            return None

    category = None
    forced_world = None
    patterns = {
        "dark": [t for t in config.TRIGGER_DARK if t],
        "common_food": [t for t in config.TRIGGER_COMMON_EAT if t],
        "common_drink": [t for t in config.TRIGGER_COMMON_DRINK if t],
        "drink": [t for t in config.TRIGGER_DRINK if t],
        "food": [t for t in config.TRIGGER_EAT if t],
    }

    def _hit(keys) -> bool:
        return any(k and k in text for k in keys)

    if _hit(patterns["dark"]):
        category = "dark"
    elif _hit(patterns["common_food"]):
        category, forced_world = "food", "common"
    elif _hit(patterns["common_drink"]):
        category, forced_world = "drink", "common"
    elif _hit(patterns["drink"]):
        category = "drink"
    elif _hit(patterns["food"]):
        category = "food"

    forced_chef = None
    chef_match = re.search(r"想和(.+?)吃饭|(.+?)特供料理|召唤(.+?)下厨", text)
    if chef_match:
        extracted = next((g for g in chef_match.groups() if g), None)
        if extracted and extracted != "黑暗":
            forced_chef = extracted.strip()
            if not category:
                category = "food"

    if not forced_world:
        for alias, w_key in st.alias_map.items():
            if category and alias in text:
                forced_world = w_key
                break
            if not category and (f"{alias}特产" in text or f"{alias}吃" in text):
                category, forced_world = "food", w_key
                break
            if not category and (f"{alias}特饮" in text or f"{alias}喝" in text):
                category, forced_world = "drink", w_key
                break

    if not category:
        return None

    await _execute_flow(_ctx, message, st, category, forced_world, forced_chef)
    return MsgSignal.BLOCK_TRIGGER


# ============================================================
# 后台下载任务
# ============================================================


async def _dlc_download_task(_ctx: AgentCtx, dlc_id: str, sha256: str, st: _State):
    try:
        ok = await shop.download_dlc(st.data_dir, dlc_id, sha256)
        if ok:
            st.reload()
            await _ctx.ms.send_text(
                _ctx.chat_key,
                f"🎉 千小妹已经把 [{dlc_id}] 搬到后厨啦！快去尝尝吧~",
                _ctx,
            )
    except Exception as e:
        logger.error(f"[千小妹商会] 进货 {dlc_id} 失败: {e}")
        try:
            await _ctx.ms.send_text(_ctx.chat_key, f"❌ 进货遭遇次元风暴: {e}", _ctx)
        except Exception:
            pass


async def _assets_download_task(_ctx: AgentCtx, st: _State):
    try:
        ok = await shop.download_base_assets(st.data_dir)
        if ok:
            st.reload()
            await _ctx.ms.send_text(_ctx.chat_key, "🎉 基础图库已安全拉取并解压部署完成！现在可以发 吃什么 开饭啦~", _ctx)
        else:
            await _ctx.ms.send_text(
                _ctx.chat_key,
                "❌ 所有镜像节点均拉取失败或校验未通过，请稍后重试，或按帮助中的网盘方式手动部署。",
                _ctx,
            )
    except Exception as e:
        logger.error(f"[千小妹商会] 基础图库下载失败: {e}")
        try:
            await _ctx.ms.send_text(_ctx.chat_key, f"❌ 图库拉取失败: {e}", _ctx)
        except Exception:
            pass


# ============================================================
# 加菜 / 上传厨师
# ============================================================


async def _handle_add_food(_ctx: AgentCtx, message: ChatMessage, text: str, st: _State):
    if not _is_admin(message):
        await _send_text(_ctx, "【越权警告】只有厨师长（管理员）可以加菜哦！若未配置管理员，请先在插件配置填写「管理员账号白名单」。")
        return

    idx = text.find("加菜 ")
    pure_args = text[idx + 3:].strip() if idx != -1 else ""
    parts = pure_args.split(maxsplit=2)
    if len(parts) < 3:
        await _send_text(_ctx, "指令格式错误！\n正确格式：加菜 [世界] [分类] [菜名]\n示例：加菜 鸣潮 饮品 冰吸生椰拿铁（请连带图片一起发送）")
        return
    world_input, cat_input, food_name = parts[0], parts[1], parts[2]

    target_world = _world_alias(st, world_input)
    if not target_world:
        await _send_text(_ctx, f"加菜失败：未识别的世界 '{world_input}'。")
        return
    target_cat = {"食物": "food", "饮品": "drink", "黑暗料理": "darkfood"}.get(cat_input)
    if not target_cat:
        await _send_text(_ctx, f"加菜失败：未识别的分类 '{cat_input}'，只能是 食物、饮品 或 黑暗料理。")
        return
    for ch in '<>:"/\\|?*':
        food_name = food_name.replace(ch, "")
    food_name = food_name.strip()
    if not food_name:
        await _send_text(_ctx, "加菜失败：菜名不合法！")
        return

    refs = _extract_image_refs(message)
    if not refs:
        await _send_text(_ctx, "加菜失败：没有检测到图片，请将图片和指令在同一条消息中发出。")
        return

    target_dir = st.data_dir / target_cat / target_world
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = await _save_images(refs, target_dir, food_name)
    if saved > 0:
        st.reload()
        await _send_text(_ctx, f"✅ 加菜成功！\n共收录 {saved} 张【{food_name}】至 {world_input} 的 {cat_input} 库中！")
    else:
        await _send_text(_ctx, "加菜失败：图片下载失败或平台限制导致无法读取。")


async def _handle_upload_chef(_ctx: AgentCtx, message: ChatMessage, text: str, st: _State):
    if not _is_admin(message):
        await _send_text(_ctx, "【越权警告】只有厨师长（管理员）可以上传厨师哦！")
        return
    idx = text.find("上传厨师")
    chef_name = text[idx + 4:].strip() if idx != -1 else ""
    for ch in '<>:"/\\|?*':
        chef_name = chef_name.replace(ch, "")
    chef_name = chef_name.strip()
    if not chef_name:
        await _send_text(_ctx, "指令格式错误！\n正确格式：上传厨师 [厨师名]\n示例：上传厨师 奥黛塔（请连带图片一起发送）")
        return

    refs = _extract_image_refs(message)
    if not refs:
        await _send_text(_ctx, "上传失败：没有检测到图片，请将图片和指令在同一条消息中发出。")
        return

    target_dir = st.data_dir / "chefs"
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = await _save_images(refs, target_dir, chef_name, start_counter=2)
    if saved > 0:
        st.reload()
        await _send_text(_ctx, f"✅ 上传厨师成功！\n共收录 {saved} 张【{chef_name}】至图库！")
    else:
        await _send_text(_ctx, "上传失败：图片下载失败或平台限制导致无法读取。")


async def _save_images(refs: List[str], target_dir: Path, base_name: str, start_counter: int = 1) -> int:
    saved = 0
    for ref in refs:
        result = await _fetch_image_bytes(ref)
        if not result:
            continue
        content, ext = result
        save_path = target_dir / f"{base_name}{ext}"
        if save_path.exists():
            counter = start_counter
            while True:
                save_path = target_dir / f"{base_name}_{counter}{ext}"
                if not save_path.exists():
                    break
                counter += 1
        try:
            save_path.write_bytes(content)
            saved += 1
        except Exception as e:
            logger.warning(f"[千小妹] 图片保存失败 {save_path}: {e}")
    return saved


# ============================================================
# 核心摇号流程
# ============================================================


async def _execute_flow(
    _ctx: AgentCtx,
    message: ChatMessage,
    st: _State,
    category: str,
    forced_world: Optional[str] = None,
    forced_chef: Optional[str] = None,
):
    cfg = st.cfg_dict
    uid = str(getattr(message, "platform_userid", "") or message.sender_id or "")
    chat_key = message.chat_key
    group_id = chat_key

    active_key = forced_world if (forced_world and forced_world != "common") else resolve_active_key(cfg.get("active_world", "世界1(鸣潮)"))
    active_conf = st.wv_settings.get(active_key, {})

    bot_pool = active_conf.get("3.自称池", [])
    bot_host = random.choice(bot_pool if bot_pool else ["推荐官"])
    world_host = active_conf.get("1.世界名称", "") or f"世界{active_key[-1]}"
    world_aliases = [a for a in active_conf.get("2.世界别称", []) if a]
    if world_aliases:
        world_host = random.choice([world_host] + world_aliases)

    # ---- 防刷屏拦截 ----
    if st.limiter.is_spaming(uid, int(cfg.get("spam_threshold", 3))):
        if random.randint(1, 100) <= int(cfg.get("interception_egg_chance", 50)):
            pool = st.ganfanren
            if pool:
                valid_names = list(pool.keys())
                egg_role = "千咲" if "千咲" in valid_names else random.choice(valid_names)
                inter_text = f"【拦截警报】你点得太快啦！{egg_role}怕你撑着，已经先你一步把厨房吃空了！"
                meme_file = st.image_mgr.get_egg_meme(egg_role)
            else:
                inter_text = "【拦截警报】你点得太快啦！系统已开启防刷屏管制！"
                meme_file = None
        else:
            inter_pool = active_conf.get("6.打断句式", [])
            inter_text = random.choice(inter_pool if inter_pool else [f"{bot_host}觉得你点得太频繁了。"]).format(bot=bot_host)
            meme_file = st.image_mgr.get_bot_meme(active_key, "speechless")
        await _send_with_meme(_ctx, st, inter_text, meme_file)
        return

    # ---- 复读摆烂 ----
    is_generic = not forced_world and not forced_chef and category != "dark"
    if (
        is_generic
        and not st.limiter.is_repeat_in_cooldown(group_id, int(cfg.get("repeat_cooldown", 60)))
        and random.randint(1, 100) <= int(cfg.get("repeat_prob", 10))
    ):
        st.limiter.record_repeat_trigger(group_id)
        if category == "food":
            fb_pool = cfg.get("eat_fallback_words", ["是啊，吃什么"])
        else:
            fb_pool = cfg.get("drink_fallback_words", ["是啊，喝什么"])
        text = random.choice(fb_pool if fb_pool else ["是啊，吃/喝什么"]).format(bot=bot_host)
        meme_file = st.image_mgr.get_bot_meme(active_key, "think")
        await _send_with_meme(_ctx, st, text, meme_file)
        return

    # ---- 空图库检测 ----
    food_dir = st.data_dir / "food"
    if not food_dir.exists() or not any(food_dir.iterdir()):
        await _send_text(_ctx, EMPTY_LIB_TEXT)
        return
    pool = list(st.image_mgr.cached_pools.get(category, []))
    if not pool:
        await _send_text(_ctx, EMPTY_LIB_TEXT)
        return

    if forced_world:
        strict = [i for i in pool if i["wv"] == forced_world]
        if strict:
            pool = strict
    if forced_chef:
        chef_pool = [i for i in pool if i.get("chef") == forced_chef]
        if not chef_pool and category == "food":
            chef_pool = [i for i in st.image_mgr.cached_pools.get("drink", []) if i.get("chef") == forced_chef]
        if not chef_pool:
            await _send_text(_ctx, f"【厨师下班】{forced_chef}今天不在厨房哦～（图库中未找到该厨师的作品）")
            return
        pool = chef_pool

    picked = st.food_mgr.filter_and_pick(group_id, pool, active_key)
    if not picked:
        await _send_text(_ctx, "【卡池告急】未找到任何可用的食物/饮品数据！请检查文件夹或配置。")
        return

    food_name = picked["food"]
    chef_name = picked["chef"]
    origin_key = picked["wv"]
    full_food_desc = f"由【{chef_name}】特制的{food_name}" if chef_name != "none" else food_name

    # ---- 文案 ----
    is_drink = category == "drink"
    is_crossover = origin_key != "common" and origin_key != active_key
    mood = "like"

    if category == "dark":
        pool_text = cfg.get("dark_drink_templates" if is_drink else "dark_templates", [])
        if not pool_text:
            pool_text = cfg.get("dark_templates", [])
        final_text = random.choice(pool_text if pool_text else ["危险的{full_food_desc}！"]).format(
            bot=bot_host, bot_a=bot_host, food=food_name, chef=chef_name,
            full_food_desc=full_food_desc, world_a=world_host,
        )
        mood = "scared"
    elif is_crossover:
        cross_conf = st.wv_settings.get(origin_key, {})
        world_b = cross_conf.get("1.世界名称", "") or "异世界"
        world_b_aliases = [a for a in cross_conf.get("2.世界别称", []) if a]
        if world_b_aliases:
            world_b = random.choice([world_b] + world_b_aliases)
        bot_b_pool = cross_conf.get("3.自称池", ["异界人"])
        bot_b = random.choice(bot_b_pool if bot_b_pool else ["异界人"])
        pool_text = cfg.get("crossover_drink_templates" if is_drink else "crossover_templates", [])
        if not pool_text:
            pool_text = cfg.get("crossover_templates", [])
        final_text = random.choice(pool_text if pool_text else ["{bot_a}遇到了{bot_b}，一起吃了{full_food_desc}"]).format(
            bot=bot_host, bot_a=bot_host, food=food_name, chef=chef_name,
            full_food_desc=full_food_desc, world_a=world_host, world_b=world_b, bot_b=bot_b,
        )
    elif chef_name != "none":
        pool_key = "12.厨师饮品句式" if is_drink else "5.厨师句式"
        pool_text = active_conf.get(pool_key, [])
        if not pool_text:
            pool_text = active_conf.get("5.厨师句式", [])
        final_text = random.choice(pool_text if pool_text else ["【{chef}】特制了{food}"]).format(
            bot=bot_host, bot_a=bot_host, food=food_name, chef=chef_name,
            full_food_desc=full_food_desc, world_a=world_host,
        )
    elif origin_key == "common":
        pool_text = cfg.get("generic_drink_templates" if is_drink else "generic_templates", [])
        if not pool_text:
            pool_text = cfg.get("generic_templates", [])
        final_text = random.choice(pool_text if pool_text else ["铛铛！为你抽中了美味的{food}！"]).format(
            bot=bot_host, bot_a=bot_host, food=food_name, chef=chef_name,
            full_food_desc=full_food_desc, world_a=world_host,
        )
    else:
        pool_key = "11.专属饮品句式" if is_drink else "4.专属句式"
        pool_text = active_conf.get(pool_key, [])
        if not pool_text:
            pool_text = active_conf.get("4.专属句式", [])
        generic_pool = cfg.get("generic_drink_templates" if is_drink else "generic_templates", [])
        if not generic_pool:
            generic_pool = cfg.get("generic_templates", [])
        combined = list(pool_text) + list(generic_pool)
        final_text = random.choice(combined if combined else ["推荐{food}"]).format(
            bot=bot_host, bot_a=bot_host, food=food_name, chef=chef_name,
            full_food_desc=full_food_desc, world_a=world_host,
        )

    if any(w in food_name for w in ("冰", "冷", "冻", "雪糕")):
        final_text = final_text.replace("热腾腾的", "冰凉的").replace("趁热吃吧", "趁凉吃吧")

    # ---- 图片：主菜 + 配菜 ----
    main_img = picked.get("path") if picked.get("has_image") else None
    meme_img = None

    if random.randint(1, 100) <= int(cfg.get("egg_prob", 10)):
        ganfanren_pool = st.ganfanren
        if ganfanren_pool:
            allowed = None
            pool_cfg = (cfg.get("egg_pool", "") or "").strip()
            if pool_cfg and pool_cfg.lower() != "random":
                allowed = [n.strip() for n in pool_cfg.replace("；", ";").split(";") if n.strip()]
            valid = list(ganfanren_pool.keys())
            if allowed:
                valid = [n for n in allowed if n in valid] or valid
            lucky = random.choice(valid)
            meme_img = random.choice(ganfanren_pool[lucky]["images"])
            words = ganfanren_pool[lucky]["words"]
            word = random.choice(words) if words else "但是所有食物被一个神秘吃货一扫而空！"
            final_text += f"\n\n{word}"
    else:
        if chef_name != "none" and random.randint(1, 100) <= int(cfg.get("chef_meme_prob", 50)):
            meme_img = st.image_mgr.get_chef_image(chef_name)
        elif random.randint(1, 100) <= int(cfg.get("global_meme_prob", 30)):
            meme_img = st.image_mgr.get_bot_meme(active_key, mood)

    images = []
    if main_img:
        images.append(main_img)
    if meme_img:
        if cfg.get("convert_meme_to_gif", True):
            meme_img = await _make_gif_copy(meme_img)
        images.append(meme_img)
    await _send_reply(_ctx, final_text, images)


async def _send_with_meme(_ctx: AgentCtx, st: _State, text: str, meme_path: Optional[str]):
    images = []
    if meme_path:
        if st.cfg_dict.get("convert_meme_to_gif", True):
            meme_path = await _make_gif_copy(meme_path)
        images.append(meme_path)
    await _send_reply(_ctx, text, images)


# ============================================================
# 
# ============================================================

from . import webapi as _webapi  # noqa: E402

_webapi.register(plugin, _st, lambda: config)
