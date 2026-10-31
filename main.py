"""astrbot_plugin_humanizer
让机器人回复更像真人：
  1. 每段回复前随机延迟（分段发送时逐段延迟）
  2. 去除尾随句号 + 拆分换行（多行拆成多条短句）
  3. 已读不回 / 选择性忽略（LLM 决策或概率模式）
  4. 加群请求处理（默认关闭）
  5. 好友申请处理（manual/auto/llm 三种模式）

仅 aiocqhttp 平台生效。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import re
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import logger as logger
except Exception:
    logger = logging.getLogger("astrbot.plugins.humanizer")


# ---------- 配置 ----------
# 每个待发送分段的额外延迟（秒）。AstrBot 自带的分段间隔仍会保留。
DELAY_MIN = 1.0
DELAY_MAX = 4.0
DELAY_PEAK = 2.0  # 三角分布峰值，多数情况延迟接近这个值
# 按分段字数增加少量时间，避免长段和短段完全一样
DELAY_PER_CHAR = 0.015
# 命令回复也加延迟？默认 False，命令是即时操作不需要模拟
DELAY_FOR_COMMAND = False
# 群聊和私聊都加延迟
DELAY_ENABLE_GROUP = True
DELAY_ENABLE_PRIVATE = True

# 延迟分布类型: "triangular"(三角) | "lognormal"(对数正态，推荐，更像真人) | "mixed"(混合，有偶发长延迟)
DELAY_DISTRIBUTION = "mixed"
# 对数正态参数（仅 distribution=lognormal 或 mixed 时生效）
DELAY_LOGNORMAL_MU = 0.65    # 位置参数，影响中位数
DELAY_LOGNORMAL_SIGMA = 0.45  # 形状参数，越大尾部越长
# mixed 模式：偶发长延迟的概率和范围
DELAY_MIXED_LONG_PROB = 0.08  # 8% 概率触发"走神/被打断"长延迟
DELAY_MIXED_LONG_MIN = 4.0
DELAY_MIXED_LONG_MAX = 9.0

# 去除尾随句号
TRIM_TRAILING_PERIOD = True
# 只对纯文本消息生效（含图片/at 的不处理，避免误伤）
TRIM_PLAIN_ONLY = True

# ---------- 已读不回 / 回复时机 决策配置 ----------
# 总开关
READ_NO_REPLY_ENABLE = True
# 决策模式: "probability"（概率）| "llm"（LLM 判断）
READ_NO_REPLY_MODE = "llm"
# 私聊和群聊是否启用
READ_NO_REPLY_PRIVATE = True
READ_NO_REPLY_GROUP = False
# 连续忽略上限：同一个会话连续忽略这么多条后，下一条一定回。0=不限制
READ_NO_REPLY_MAX_SKIP = 8

# --- 概率模式配置 ---
READ_NO_REPLY_PROB_DAY = 0.08       # 白天不回概率
READ_NO_REPLY_PROB_NIGHT = 0.35     # 深夜不回概率
READ_NO_REPLY_DAY_HOURS = (8, 23)   # 白天时段

# --- LLM 模式配置 ---
# 指定用于决策的 LLM provider ID。留空 "" 则使用当前会话的默认 LLM。
# 在 WebUI 的 模型管理 页面可以看到每个 provider 的 ID。
READ_NO_REPLY_LLM_PROVIDER_ID = ""
# LLM 超时（秒），超时则回退到概率模式
READ_NO_REPLY_LLM_TIMEOUT = 8
# 决策超时后用概率模式兜底
READ_NO_REPLY_LLM_FALLBACK_TO_PROB = True
# 是否把对方连续未回复的条数告诉 LLM（帮助判断是否急件）
READ_NO_REPLY_LLM_INCLUDE_SKIP_COUNT = True

READ_NO_REPLY_LLM_SYSTEM_PROMPT = """你是达妮娅的潜意识判断层。你需要决定：收到这条消息后，达妮娅要不要现在回复，以及如果回复的话大概等多久。

背景：
- 达妮娅是一个真实的人，在星炬学院上学。她不是随时盯着手机的。
- 她会困、会忙、会走神、会看到消息先放着。
- 但如果对方明显很着急、连发很多条、或者说的是很重要的事，她会回得快一些甚至立刻回。
- 深夜她大概率在睡觉，可能不回。

请根据以下信息做判断，只输出 JSON，不要输出任何其他内容：
{"reply": true或false, "delay_seconds": 数字, "reason": "简短原因"}

字段说明：
- reply: 是否回复。true=回复，false=不回（已读不回）。
- delay_seconds: 回复前等待多少秒。0=立刻回。建议 0~3600。
- reason: 一句话说明为什么这么判断。

判断规则：
- 对方连发多条、语气急切、提到紧急/救命/出事了、问"在吗"后又追问 → reply=true, delay_seconds 较小（0~10）。
- 深夜（24:00-08:00）、对方只是闲聊/分享日常、语气平缓 → 可以不回或延迟较久。
- 对方问问题、需要回应 → 应该回，但可以延迟。
- 对方只是发了个表情包/语气词 → 可以不回。
"""

# 加群请求处理模式: "off" | "auto_accept" | "auto_reject" | "notify_admin"
GROUP_REQUEST_MODE = "off"
# 管理员 QQ 号（notify_admin 模式下通知谁），字符串列表
ADMIN_IDS: list[str] = ["2196983736"]

# ---------- 好友申请处理 ----------
# 模式: "manual"（手动，通知管理员）| "auto"（自动同意）| "llm"（LLM 判断，不确定时询问管理员）
FRIEND_REQUEST_MODE = "llm"
# LLM 模式用的 provider ID，留空用默认
FRIEND_REQUEST_LLM_PROVIDER_ID = ""
# LLM 超时秒
FRIEND_REQUEST_LLM_TIMEOUT = 10
# LLM 判断系统提示
FRIEND_REQUEST_LLM_SYSTEM_PROMPT = """你是达妮娅的社交安全判断层。有人申请加达妮娅为 QQ 好友，你需要严格决定是否同意。

背景：
- 达妮娅是一个真实的女大学生，在星炬学院上学。
- 她只加认识的人：同学、朋友、家人，或者能明确说出共同身份/共同朋友的人。
- 陌生人、含糊其辞的、疑似营销/诈骗/色情的，一律拒绝。
- 宁可拒绝也不要放过垃圾账号。拿不准就拒绝，绝不为了"礼貌"而同意。

请根据申请人 QQ 号和验证消息做判断，只输出 JSON，不要输出其他内容：
{"decision": "accept"或"reject"或"ask", "reason": "简短原因"}

严格判断规则（按优先级）：
1. 验证消息包含以下任一特征 → reject：
   - 广告、刷单、兼职、返利、加微/加v、扫码、免费领、福利、色/约/黄/资源
   - "你好"、"在吗"、"加个好友"、"认识一下"等无实质内容的打招呼
   - 空验证消息或纯表情/纯符号
   - 明显的推广语、诱导点击、带链接
   - 称呼"美女"、"小姐姐"等搭讪式语气
2. 验证消息同时满足以下全部条件 → accept：
   - 明确说出了达妮娅的真实身份（名字/学校/专业/班级/宿舍等）
   - 或者明确说出了共同朋友/共同经历
   - 语气自然，不像机器人或营销
3. 其他情况 → reject（默认拒绝，不主动问管理员）
4. 只有当验证消息提到了达妮娅但信息模糊到无法确认（比如只说"我是你同学"但不说名字）时 → ask

注意：默认拒绝是安全的，不要轻易 accept。"""


@register("humanizer", "AI", "拟人化助手", "1.6.0")
class Humanizer(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.delay_min = DELAY_MIN
        self.delay_max = DELAY_MAX
        self.delay_peak = DELAY_PEAK
        self.delay_per_char = DELAY_PER_CHAR
        self.delay_for_command = DELAY_FOR_COMMAND
        self.delay_enable_group = DELAY_ENABLE_GROUP
        self.delay_enable_private = DELAY_ENABLE_PRIVATE
        self.delay_distribution = DELAY_DISTRIBUTION
        self.delay_lognormal_mu = DELAY_LOGNORMAL_MU
        self.delay_lognormal_sigma = DELAY_LOGNORMAL_SIGMA
        self.delay_mixed_long_prob = DELAY_MIXED_LONG_PROB
        self.delay_mixed_long_min = DELAY_MIXED_LONG_MIN
        self.delay_mixed_long_max = DELAY_MIXED_LONG_MAX

        self.trim_trailing_period = TRIM_TRAILING_PERIOD
        self.trim_plain_only = TRIM_PLAIN_ONLY

        self.read_no_reply_enable = READ_NO_REPLY_ENABLE
        self.read_no_reply_mode = READ_NO_REPLY_MODE
        self.read_no_reply_private = READ_NO_REPLY_PRIVATE
        self.read_no_reply_group = READ_NO_REPLY_GROUP
        self.read_no_reply_max_skip = READ_NO_REPLY_MAX_SKIP
        self.read_no_reply_prob_day = READ_NO_REPLY_PROB_DAY
        self.read_no_reply_prob_night = READ_NO_REPLY_PROB_NIGHT
        self.read_no_reply_day_hours = READ_NO_REPLY_DAY_HOURS
        self.read_no_reply_llm_timeout = READ_NO_REPLY_LLM_TIMEOUT
        self.read_no_reply_llm_fallback = READ_NO_REPLY_LLM_FALLBACK_TO_PROB
        self.read_no_reply_llm_include_skip = READ_NO_REPLY_LLM_INCLUDE_SKIP_COUNT
        self.read_no_reply_llm_provider_id = READ_NO_REPLY_LLM_PROVIDER_ID
        # 记录每个会话连续忽略次数: session_id -> count
        self._skip_count: dict[str, int] = {}

        self.group_request_mode = GROUP_REQUEST_MODE
        self.admin_ids = list(ADMIN_IDS)

        self.friend_request_mode = FRIEND_REQUEST_MODE
        self.friend_request_llm_provider_id = FRIEND_REQUEST_LLM_PROVIDER_ID
        self.friend_request_llm_timeout = FRIEND_REQUEST_LLM_TIMEOUT
        # 最近一次好友申请的 flag，方便手动同意时省略 flag
        self._last_friend_flag = ""
        # 待管理员处理的好友申请: flag -> {uid, comment, flag}
        self._pending_friend_requests: dict[str, dict] = {}

    # ==================== LLM 工具实现 ====================
    async def _get_bot_from_event(self, event: AstrMessageEvent):
        return getattr(event, "bot", None)

    def _check_tool_permission(self, event: AstrMessageEvent) -> str | None:
        """检查工具调用权限，只有管理员可以调用好友管理工具。返回 None 表示有权限，否则返回拒绝提示。"""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        if sender_id and sender_id in self.admin_ids:
            return None
        # 没有配置管理员时，默认拒绝（避免任何人都能操作）
        return "抱歉，你没有权限执行这个操作。只有管理员才能管理好友列表和好友申请。"

    @filter.llm_tool(name="friend_list")
    async def tool_friend_list(self, event: AstrMessageEvent) -> str:
        """获取当前 QQ 账号的好友列表，返回每个好友的 QQ 号和昵称。当用户想知道自己有哪些好友、或者想找某个好友时调用。"""
        denied = self._check_tool_permission(event)
        if denied:
            return denied
        bot = await self._get_bot_from_event(event)
        if not bot:
            return "当前平台不支持好友列表"
        try:
            ret = await bot.call_action("get_friend_list")
            if not ret:
                return "好友列表为空"
            lines = [f"好友列表（共{len(ret)}人）："]
            for f in ret[:50]:
                uid = f.get("user_id", "?")
                nick = f.get("nickname", "") or f.get("remark", "") or "未知"
                lines.append(f"{uid} - {nick}")
            if len(ret) > 50:
                lines.append(f"...还有 {len(ret)-50} 个好友")
            return "\n".join(lines)
        except Exception as e:
            return f"获取好友列表失败: {e}"

    @filter.llm_tool(name="friend_delete")
    async def tool_friend_delete(self, event: AstrMessageEvent, user_id: str = "") -> str:
        """删除指定 QQ 号的好友。当用户明确要求删除某个好友时调用。注意：删除是不可逆操作，需谨慎。

        Args:
            user_id(string): 要删除的好友 QQ 号。
        """
        denied = self._check_tool_permission(event)
        if denied:
            return denied
        if not user_id:
            return "请提供要删除的好友 QQ 号"
        bot = await self._get_bot_from_event(event)
        if not bot:
            return "当前平台不支持"
        try:
            await bot.call_action("delete_friend", user_id=int(user_id))
            return f"已删除好友 {user_id}"
        except Exception as e:
            return f"删除失败: {e}"

    @filter.llm_tool(name="friend_stranger_info")
    async def tool_friend_stranger_info(self, event: AstrMessageEvent, user_id: str = "") -> str:
        """查询某个 QQ 号的陌生人信息（昵称、性别、年龄）。当用户想了解某个 QQ 号是谁时调用。

        Args:
            user_id(string): 要查询的 QQ 号。
        """
        denied = self._check_tool_permission(event)
        if denied:
            return denied
        if not user_id:
            return "请提供要查询的 QQ 号"
        bot = await self._get_bot_from_event(event)
        if not bot:
            return "当前平台不支持"
        try:
            ret = await bot.call_action("get_stranger_info", user_id=int(user_id), no_cache=False)
            if ret:
                uid = ret.get("user_id", user_id)
                nick = ret.get("nickname", "未知")
                sex_map = {"male": "男", "female": "女", "unknown": "未知"}
                sex = sex_map.get(ret.get("sex", "unknown"), "未知")
                age = ret.get("age", "未知")
                return f"QQ: {uid}\n昵称: {nick}\n性别: {sex}\n年龄: {age}"
            return f"未找到用户 {user_id} 的信息"
        except Exception as e:
            return f"查询失败: {e}"

    @filter.llm_tool(name="friend_pending_requests")
    async def tool_friend_pending_requests(self, event: AstrMessageEvent) -> str:
        """查看当前待处理的好友申请列表（包含申请人 QQ、验证消息、flag）。当用户问有没有人加她、或者想处理好友申请时调用。"""
        denied = self._check_tool_permission(event)
        if denied:
            return denied
        if not self._pending_friend_requests:
            return "当前没有待处理的好友申请"
        lines = ["待处理好友申请："]
        for flag, info in self._pending_friend_requests.items():
            lines.append(f"QQ: {info['uid']} | 验证: {info['comment']} | flag: {flag}")
        return "\n".join(lines)

    @filter.llm_tool(name="friend_accept")
    async def tool_friend_accept(self, event: AstrMessageEvent, flag: str = "", remark: str = "") -> str:
        """同意指定的好友申请。当用户决定同意某个好友申请时调用。

        Args:
            flag(string): 好友申请的 flag 值，从 friend_pending_requests 获取。留空则同意最近一次申请。
            remark(string): 可选，给对方设置的备注名。
        """
        denied = self._check_tool_permission(event)
        if denied:
            return denied
        if not flag:
            flag = self._last_friend_flag
        if not flag:
            return "最近没有好友申请"
        bot = await self._get_bot_from_event(event)
        if not bot:
            return "当前平台不支持"
        try:
            params = {"flag": flag, "approve": True}
            if remark:
                params["remark"] = remark
            await bot.call_action("set_friend_add_request", **params)
            self._pending_friend_requests.pop(flag, None)
            return f"已同意好友申请 (flag={flag})"
        except Exception as e:
            return f"操作失败: {e}"

    @filter.llm_tool(name="friend_reject")
    async def tool_friend_reject(self, event: AstrMessageEvent, flag: str = "", reason: str = "") -> str:
        """拒绝指定的好友申请。当用户决定拒绝某个好友申请时调用。

        Args:
            flag(string): 好友申请的 flag 值，从 friend_pending_requests 获取。留空则拒绝最近一次申请。
            reason(string): 可选，拒绝理由。
        """
        denied = self._check_tool_permission(event)
        if denied:
            return denied
        if not flag:
            flag = self._last_friend_flag
        if not flag:
            return "最近没有好友申请"
        bot = await self._get_bot_from_event(event)
        if not bot:
            return "当前平台不支持"
        try:
            params = {"flag": flag, "approve": False}
            if reason:
                params["remark"] = reason
            await bot.call_action("set_friend_add_request", **params)
            self._pending_friend_requests.pop(flag, None)
            return f"已拒绝好友申请 (flag={flag})"
        except Exception as e:
            return f"操作失败: {e}"

    # ==================== 0. 已读不回 / 回复时机 ====================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_read_no_reply(self, event: AstrMessageEvent):
        """收到消息时决定是否回复、延迟多久。支持 LLM 决策或概率模式。"""
        if not self.read_no_reply_enable:
            return
        if not self._is_aiocqhttp(event):
            return

        is_group = bool(event.get_group_id())
        if is_group and not self.read_no_reply_group:
            return
        if not is_group and not self.read_no_reply_private:
            return

        session_id = event.get_group_id() if is_group else event.get_sender_id()
        if not session_id:
            return

        msg_text = (event.message_str or "").strip()

        # 命令永远回复（操作指令必须即时响应）
        if msg_text.startswith("/"):
            self._skip_count[session_id] = 0
            return

        # 连续忽略次数达到上限，强制回复
        skipped = self._skip_count.get(session_id, 0)
        if self.read_no_reply_max_skip > 0 and skipped >= self.read_no_reply_max_skip:
            self._skip_count[session_id] = 0
            return

        if self.read_no_reply_mode == "llm":
            should_reply, delay = await self._llm_decide_reply(event, msg_text, session_id, skipped)
            if should_reply is None:
                # LLM 失败，回退概率模式
                if self.read_no_reply_llm_fallback:
                    if not self._probability_should_reply():
                        self._skip_count[session_id] = skipped + 1
                        event.stop_event()
                        return
                # 不回退就放行
                self._skip_count[session_id] = 0
                return
            if not should_reply:
                self._skip_count[session_id] = skipped + 1
                event.stop_event()
                return
            if delay > 0:
                await asyncio.sleep(min(delay, 600))
            self._skip_count[session_id] = 0
        else:
            # 概率模式
            if not self._probability_should_reply():
                self._skip_count[session_id] = skipped + 1
                event.stop_event()
                return
            self._skip_count[session_id] = 0

    def _probability_should_reply(self) -> bool:
        """概率模式：按时间段随机决定是否回复。返回 True=回复。"""
        now_hour = time.localtime().tm_hour
        day_start, day_end = self.read_no_reply_day_hours
        prob = self.read_no_reply_prob_day if day_start <= now_hour < day_end else self.read_no_reply_prob_night
        return random.random() >= prob

    async def _llm_decide_reply(self, event: AstrMessageEvent, msg_text: str, session_id: str, skipped: int) -> tuple[bool | None, float]:
        """调用 LLM 判断是否回复、延迟多久。返回 (should_reply, delay_seconds)。失败返回 (None, 0)。"""
        try:
            provider = await self._get_provider(event)
            if provider is None:
                return None, 0
            text_chat = getattr(provider, "text_chat", None)
            if not callable(text_chat):
                return None, 0

            now = time.localtime()
            time_str = f"{now.tm_hour:02d}:{now.tm_min:02d}"
            skip_info = f"\n对方已连续 {skipped} 条消息未得到回复" if skipped > 0 and self.read_no_reply_llm_include_skip else ""

            prompt = (
                f"当前时间: {time_str}\n"
                f"消息内容: {msg_text}\n"
                f"{skip_info}\n"
                f"请输出 JSON。"
            )

            response = await asyncio.wait_for(
                self._call_text_chat(text_chat, prompt),
                timeout=self.read_no_reply_llm_timeout,
            )
            decision = self._parse_llm_decision(response)
            if decision is None:
                return None, 0
            return decision.get("reply", True), float(decision.get("delay_seconds", 0))
        except (asyncio.TimeoutError, Exception):
            return None, 0

    async def _get_provider(self, event: AstrMessageEvent):
        """复用 astrbot 的 LLM provider。优先用配置的 provider_id，否则用当前会话默认。"""
        context = getattr(self, "context", None) or getattr(self, "_context", None)
        if context is None:
            return None
        # 指定了 provider ID 就用它
        if self.read_no_reply_llm_provider_id:
            getter = getattr(context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = getter(self.read_no_reply_llm_provider_id)
                    if inspect.isawaitable(provider):
                        provider = await provider
                    if provider is not None:
                        return provider
                except Exception:
                    pass
        # 否则用当前会话默认 provider
        getter = getattr(context, "get_using_provider", None)
        if not callable(getter):
            return None
        umo = getattr(event, "unified_msg_origin", None)
        for args, kwargs in [((), {"umo": umo}), ((umo,), {}), ((), {})]:
            try:
                provider = getter(*args, **kwargs)
                if inspect.isawaitable(provider):
                    provider = await provider
                if provider is not None:
                    return provider
            except TypeError:
                continue
            except Exception:
                break
        return None

    @staticmethod
    async def _call_text_chat(text_chat, prompt: str, system_prompt: str = READ_NO_REPLY_LLM_SYSTEM_PROMPT) -> str:
        """调用 provider.text_chat，兼容不同签名。"""
        attempts = [
            {"prompt": prompt, "contexts": [], "system_prompt": system_prompt},
            {"prompt": prompt, "context": [], "system_prompt": system_prompt},
            {"prompt": prompt},
        ]
        for kwargs in attempts:
            try:
                response = text_chat(**kwargs)
                if inspect.isawaitable(response):
                    response = await response
                if response is None:
                    continue
                for attr in ("completion_text", "text", "content", "message"):
                    val = getattr(response, attr, None)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                if isinstance(response, dict):
                    for key in ("completion_text", "text", "content", "message"):
                        val = response.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
                if isinstance(response, str):
                    return response.strip()
            except TypeError:
                continue
        return ""

    @staticmethod
    def _parse_llm_decision(text: str) -> dict | None:
        """从 LLM 输出中解析 JSON 决策。"""
        if not text:
            return None
        # 去掉可能的代码块包裹
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        # 尝试直接解析
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "reply" in data:
                return data
        except Exception:
            pass
        # 尝试提取 JSON 片段
        match = re.search(r"\{[^{}]*\"reply\"[^{}]*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, dict) and "reply" in data:
                    return data
            except Exception:
                pass
        return None

    # ==================== 1. 分段延迟 + 去除尾随句号 ====================
    @filter.on_decorating_result()
    async def on_decorating_trim_period(self, event: AstrMessageEvent):
        """为每次发送单独延迟，并在发送前去掉 LLM 回复尾随标点。"""
        if not self._is_aiocqhttp(event):
            return

        result = event.get_result()
        if result is None:
            return

        if result.is_llm_result():
            is_group = bool(event.get_group_id())
            should_delay = (
                self.delay_enable_group if is_group else self.delay_enable_private
            )
            if should_delay and not event.get_extra("_humanizer_send_wrapped", False):
                original_send = event.send

                async def delayed_send(message_chain, *args, **kwargs):
                    text = self._extract_chain_text(message_chain)
                    delay = self._sample_delay()
                    if self.delay_per_char > 0 and text:
                        delay += min(len(text) * self.delay_per_char, self.delay_max)
                    await asyncio.sleep(delay)
                    return await original_send(message_chain, *args, **kwargs)

                event.send = delayed_send
                event.set_extra("_humanizer_send_wrapped", True)

        # 只处理 LLM 结果，不处理命令回复
        if not result.is_llm_result():
            return

        chain = result.chain
        if not chain:
            return

        # 如果只处理纯文本，检查 chain 是否全是 Plain
        if self.trim_plain_only:
            if not all(isinstance(c, Plain) for c in chain):
                return

        # 把含换行的 Plain 拆成多个独立 Plain，并去掉每个 Plain 末尾的句号
        trailing_re = re.compile(r"[.。！!？?\s]+$")
        new_chain = []
        for comp in chain:
            if isinstance(comp, Plain):
                # 处理所有换行符（\n、\r、\r\n、多个连续换行）
                parts = [p.strip() for p in re.split(r"[\r\n]+", comp.text)]
                for part in parts:
                    if not part:
                        continue
                    if self.trim_trailing_period:
                        part = trailing_re.sub("", part)
                    if part:
                        new_chain.append(Plain(part))
            else:
                new_chain.append(comp)
        result.chain = new_chain

    # ==================== 3. 加群请求处理 ====================
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_request(self, event: AstrMessageEvent):
        """监听加群请求事件（OneBot v11 request_type=group）。"""
        try:
            await self._handle_group_request(event)
        except Exception as e:
            logger.error(f"[humanizer] 处理加群请求时出错: {e}", exc_info=True)

    async def _handle_group_request(self, event: AstrMessageEvent):
        if self.group_request_mode == "off":
            return

        raw = getattr(event.message_obj, "raw_message", None)
        if not raw or not isinstance(raw, dict):
            return
        if raw.get("post_type") != "request":
            return
        if raw.get("request_type") != "group":
            return

        flag = raw.get("flag", "")
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        sub_type = raw.get("sub_type", "")  # add / invite
        comment = raw.get("comment", "")

        bot = getattr(event, "bot", None)
        if not bot or not flag:
            return

        if self.group_request_mode == "auto_accept":
            try:
                await bot.call_action(
                    "set_group_add_request",
                    flag=flag,
                    sub_type=sub_type or "add",
                    approve=True,
                )
                logger.info(f"[humanizer] 自动同意加群请求 群:{group_id} 申请人:{user_id}")
            except Exception as e:
                logger.error(f"[humanizer] 自动同意加群失败: {e}")

        elif self.group_request_mode == "auto_reject":
            try:
                await bot.call_action(
                    "set_group_add_request",
                    flag=flag,
                    sub_type=sub_type or "add",
                    approve=False,
                    reason="",
                )
                logger.info(f"[humanizer] 自动拒绝加群请求 群:{group_id} 申请人:{user_id}")
            except Exception as e:
                logger.error(f"[humanizer] 自动拒绝加群失败: {e}")

        elif self.group_request_mode == "notify_admin":
            msg = (
                f"收到加群请求\n"
                f"群号: {group_id}\n"
                f"申请人: {user_id}\n"
                f"类型: {'邀请入群' if sub_type == 'invite' else '申请入群'}\n"
                f"验证: {comment}\n"
                f"同意: /approve_group {flag} {sub_type}\n"
                f"拒绝: /reject_group {flag} {sub_type}"
            )
            logger.info(f"[humanizer] {msg}")
            for admin in self.admin_ids:
                try:
                    await bot.call_action(
                        "send_private_msg",
                        user_id=int(admin),
                        message=msg,
                    )
                except Exception:
                    pass

    # ==================== 4. 好友申请处理 ====================
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_friend_request(self, event: AstrMessageEvent):
        """监听好友申请事件（OneBot v11 request_type=friend）。"""
        try:
            await self._handle_friend_request(event)
        except Exception as e:
            # 好友申请事件绝对不能抛异常，否则 AstrBot 会把错误消息发给申请人（非好友），导致发送失败
            logger.error(f"[humanizer] 处理好友申请时出错: {e}", exc_info=True)

    async def _handle_friend_request(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", None)
        if not raw or not isinstance(raw, dict):
            return
        if raw.get("post_type") != "request":
            return
        if raw.get("request_type") != "friend":
            return

        uid = str(raw.get("user_id", "?"))
        comment = raw.get("comment", "") or ""
        flag_val = raw.get("flag", "")
        self._last_friend_flag = flag_val
        self._pending_friend_requests[flag_val] = {
            "uid": uid,
            "comment": comment,
            "flag": flag_val,
        }

        bot = getattr(event, "bot", None)
        if not bot:
            return

        if self.friend_request_mode == "auto":
            # 自动同意，不回复申请人（非好友无法发送）
            try:
                await bot.call_action("set_friend_add_request", flag=flag_val, approve=True)
                logger.info(f"[humanizer] 自动同意好友申请: {uid} 验证消息: {comment}")
                self._pending_friend_requests.pop(flag_val, None)
            except Exception as e:
                if self._is_no_such_request_error(e):
                    logger.info(f"[humanizer] 好友申请已过期（已被处理）: {uid}")
                    self._pending_friend_requests.pop(flag_val, None)
                else:
                    logger.error(f"[humanizer] 自动同意好友申请失败: {uid} error={e}")
            return

        # manual 或 llm 模式都需要通知管理员
        if self.friend_request_mode == "llm":
            decision = await self._llm_decide_friend_request(uid, comment)
            if decision == "accept":
                try:
                    await bot.call_action("set_friend_add_request", flag=flag_val, approve=True)
                    logger.info(f"[humanizer] LLM同意好友申请: {uid} 验证消息: {comment}")
                    self._pending_friend_requests.pop(flag_val, None)
                    return
                except Exception as e:
                    if self._is_no_such_request_error(e):
                        logger.info(f"[humanizer] 好友申请已过期（已被处理）: {uid}")
                        self._pending_friend_requests.pop(flag_val, None)
                        return
                    logger.error(f"[humanizer] LLM同意好友申请失败: {uid} error={e}")
            elif decision == "reject":
                try:
                    await bot.call_action("set_friend_add_request", flag=flag_val, approve=False)
                    logger.info(f"[humanizer] LLM拒绝好友申请: {uid} 验证消息: {comment}")
                    self._pending_friend_requests.pop(flag_val, None)
                    return
                except Exception as e:
                    if self._is_no_such_request_error(e):
                        logger.info(f"[humanizer] 好友申请已过期（已被处理）: {uid}")
                        self._pending_friend_requests.pop(flag_val, None)
                        return
                    logger.error(f"[humanizer] LLM拒绝好友申请失败: {uid} error={e}")
            # decision == "ask" 或 LLM 失败 → 通知管理员

        # 通知管理员
        await self._notify_admins_friend_request(bot, uid, comment, flag_val)

    async def _llm_decide_friend_request(self, uid: str, comment: str) -> str:
        """LLM 判断是否同意好友申请。返回 accept/reject/ask。失败返回 ask。"""
        try:
            provider = await self._get_provider_by_id(self.friend_request_llm_provider_id)
            if provider is None:
                return "ask"
            text_chat = getattr(provider, "text_chat", None)
            if not callable(text_chat):
                return "ask"

            prompt = f"申请人QQ: {uid}\n验证消息: {comment}\n请输出JSON。"
            response = await asyncio.wait_for(
                self._call_text_chat(text_chat, prompt, FRIEND_REQUEST_LLM_SYSTEM_PROMPT),
                timeout=self.friend_request_llm_timeout,
            )
            decision = self._parse_friend_decision(response)
            return decision or "ask"
        except (asyncio.TimeoutError, Exception):
            return "ask"

    async def _get_provider_by_id(self, provider_id: str):
        """获取指定 provider，空则用默认。"""
        context = getattr(self, "context", None) or getattr(self, "_context", None)
        if context is None:
            return None
        if provider_id:
            getter = getattr(context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = getter(provider_id)
                    if inspect.isawaitable(provider):
                        provider = await provider
                    if provider is not None:
                        return provider
                except Exception:
                    pass
        getter = getattr(context, "get_using_provider", None)
        if not callable(getter):
            return None
        for args, kwargs in [((), {}), (("default",), {})]:
            try:
                provider = getter(*args, **kwargs)
                if inspect.isawaitable(provider):
                    provider = await provider
                if provider is not None:
                    return provider
            except TypeError:
                continue
            except Exception:
                break
        return None

    @staticmethod
    def _parse_friend_decision(text: str) -> str | None:
        """从 LLM 输出解析好友申请决策。"""
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                d = str(data.get("decision", "")).lower()
                if d in ("accept", "reject", "ask"):
                    return d
        except Exception:
            pass
        match = re.search(r'"decision"\s*:\s*"(accept|reject|ask)"', text, re.IGNORECASE)
        if match:
            return match.group(1).lower()
        # 关键词兜底
        low = text.lower()
        if "reject" in low or "拒绝" in text:
            return "reject"
        if "accept" in low or "同意" in text:
            return "accept"
        return None

    async def _notify_admins_friend_request(self, bot, uid: str, comment: str, flag_val: str):
        """给所有管理员发私聊通知好友申请。"""
        msg = (
            f"收到好友申请\n"
            f"申请人: {uid}\n"
            f"验证消息: {comment}\n"
            f"同意: /accept_friend {flag_val}\n"
            f"拒绝: /reject_friend {flag_val}"
        )
        logger.info(f"[humanizer] {msg}")
        for admin in self.admin_ids:
            try:
                await bot.call_action(
                    "send_private_msg",
                    user_id=int(admin),
                    message=msg,
                )
            except Exception:
                pass

    @staticmethod
    def _is_no_such_request_error(e: Exception) -> bool:
        """判断异常是否为'No such request'（好友申请已过期/已被处理）。"""
        # ActionFailed 的消息在 result.message / result.wording 里
        result = getattr(e, "result", None)
        for attr in ("message", "wording"):
            val = getattr(result, attr, None)
            if val and "No such request" in str(val):
                return True
        # 兜底：直接看 str
        if "No such request" in str(e):
            return True
        return False

    @filter.command("accept_friend")
    async def cmd_accept_friend(self, event: AstrMessageEvent, flag: str = "", remark: str = ""):
        """同意好友申请 用法: /accept_friend [flag值] [备注名]"""
        bot = getattr(event, "bot", None)
        if not bot:
            yield event.plain_result("当前平台不支持")
            return
        if not flag:
            flag = self._last_friend_flag
        if not flag:
            yield event.plain_result("最近未收到好友申请")
            return
        try:
            params = {"flag": flag, "approve": True}
            if remark:
                params["remark"] = remark
            await bot.call_action("set_friend_add_request", **params)
            self._pending_friend_requests.pop(flag, None)
            yield event.plain_result(f"已同意好友申请 (flag={flag})")
        except Exception as e:
            if self._is_no_such_request_error(e):
                # 请求已过期或已被处理，检查对方是否已是好友
                pending = self._pending_friend_requests.get(flag)
                uid = pending.get("uid") if pending else ""
                already_friend = False
                if uid:
                    try:
                        friend_list = await bot.call_action("get_friend_list") or []
                        already_friend = any(str(f.get("user_id")) == str(uid) for f in friend_list)
                    except Exception:
                        pass
                self._pending_friend_requests.pop(flag, None)
                if already_friend:
                    yield event.plain_result(f"该好友申请已被处理（{uid} 已经是你的好友了）")
                else:
                    yield event.plain_result(
                        f"好友申请已过期或不存在 (flag={flag})\n"
                        f"可能原因：申请已在QQ客户端处理、或请求列表已刷新。\n"
                        f"请让对方重新发送好友申请。"
                    )
            else:
                yield event.plain_result(f"操作失败: {e}")

    @filter.command("reject_friend")
    async def cmd_reject_friend(self, event: AstrMessageEvent, flag: str = "", reason: str = ""):
        """拒绝好友申请 用法: /reject_friend [flag值] [拒绝理由]"""
        bot = getattr(event, "bot", None)
        if not bot:
            yield event.plain_result("当前平台不支持")
            return
        if not flag:
            flag = self._last_friend_flag
        if not flag:
            yield event.plain_result("最近未收到好友申请")
            return
        try:
            params = {"flag": flag, "approve": False}
            if reason:
                params["remark"] = reason
            await bot.call_action("set_friend_add_request", **params)
            self._pending_friend_requests.pop(flag, None)
            yield event.plain_result(f"已拒绝好友申请 (flag={flag})")
        except Exception as e:
            if self._is_no_such_request_error(e):
                self._pending_friend_requests.pop(flag, None)
                yield event.plain_result(
                    f"好友申请已过期或不存在 (flag={flag})\n"
                    f"可能已被处理或请求列表已刷新。"
                )
            else:
                yield event.plain_result(f"操作失败: {e}")

    @filter.command("friend_request_mode")
    async def cmd_friend_request_mode(self, event: AstrMessageEvent, mode: str = ""):
        """设置好友申请处理模式 用法: /friend_request_mode manual|auto|llm|status"""
        if not mode or mode == "status":
            yield event.plain_result(f"好友申请处理模式: {self.friend_request_mode}")
            return
        if mode in ("manual", "auto", "llm"):
            self.friend_request_mode = mode
            yield event.plain_result(f"好友申请处理模式已设为: {mode}")
        else:
            yield event.plain_result("用法: /friend_request_mode manual|auto|llm|status")

    @filter.command("judge_friend")
    async def cmd_judge_friend(self, event: AstrMessageEvent, qq: str = "", comment: str = ""):
        """用 LLM 判断一个好友申请是否该同意（用于重判/测试）。用法: /judge_friend <QQ号> <验证消息>"""
        if not qq:
            yield event.plain_result("用法: /judge_friend <QQ号> <验证消息>")
            return
        decision = await self._llm_decide_friend_request(qq, comment)
        reason_map = {"accept": "建议同意", "reject": "建议拒绝", "ask": "拿不准，建议手动决定"}
        yield event.plain_result(
            f"申请人: {qq}\n验证消息: {comment}\nLLM判断: {reason_map.get(decision, decision)}"
        )

    # ==================== 辅助方法 ====================
    def _sample_delay(self) -> float:
        """采样一次发送延迟（秒）。支持三角/对数正态/混合分布。

        真实人类回复延迟特征：
        - 多数在 1-3 秒（思考+打字）
        - 偶尔较长（走神、被打断）
        - 分布右偏，众数偏低
        对数正态和混合分布比对称的三角分布更自然。
        """
        dist = self.delay_distribution
        if dist == "lognormal":
            delay = random.lognormvariate(self.delay_lognormal_mu, self.delay_lognormal_sigma)
            return max(self.delay_min, min(delay, self.delay_max))
        if dist == "mixed":
            # 小概率触发长延迟（走神/被打断）
            if random.random() < self.delay_mixed_long_prob:
                return random.uniform(self.delay_mixed_long_min, self.delay_mixed_long_max)
            delay = random.lognormvariate(self.delay_lognormal_mu, self.delay_lognormal_sigma)
            return max(self.delay_min, min(delay, self.delay_max))
        # 默认三角分布
        return random.triangular(self.delay_min, self.delay_max, self.delay_peak)

    @staticmethod
    def _is_aiocqhttp(event: AstrMessageEvent) -> bool:
        meta = getattr(event, "platform_meta", None)
        if meta is None:
            return False
        name = getattr(meta, "name", "") or ""
        return "aiocqhttp" in name.lower()

    @staticmethod
    def _extract_chain_text(message_chain) -> str:
        """从本次待发送的消息链提取文本，分段发送时通常只含一个分段。"""
        if message_chain is None:
            return ""
        # MessageChain 对象用 .chain 访问组件列表；也兼容直接传 list 的情况
        components = getattr(message_chain, "chain", message_chain)
        if not components:
            return ""
        return "".join(
            component.text
            for component in components
            if isinstance(component, Plain)
        )
