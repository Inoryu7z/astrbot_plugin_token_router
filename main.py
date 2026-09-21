"""
astrbot_plugin_token_router - Token用量追踪与模型路由插件

追踪每个对话窗口(UMO)的token用量，当某个模型的每日用量达到限额时，
自动切换到路由链中的下一个模型。当所有模型都达到限额时，回退到框架默认模型。
每天0点(本地时间)自动重置用量计数。

支持两种统计模式：
- window: 每个窗口独立计数，互不影响
- global: 所有窗口共享同一provider的用量计数，任一窗口的请求都会累加

v1.1.0 新增：基于人格(persona)的路由。同一UMO下可配置多个窗口，
每个窗口绑定不同人格ID，实现多人格各自独立的路由链与用量计数。

v1.4.0 新增：DB用量校准。框架的 LLM 响应钩子每条消息只携带最终一步的
usage，多步 agent（工具调用等）中间步的消耗钩子看不到，导致本地桶漏计。
开启校准后从框架 provider_stats 表读取当日各 provider 真实总量
（与 WebUI 面板同源），把差值按各作用域纯聊天量占比分摊进路由判定用量。

v1.5.0 新增：多模态顺延。每个窗口可选开启，两种模式：
- 正向顺延：消息带图片而当前模型不支持图片输入时，往后顺延到第一个
  「支持图片且当日未达限额」的模型，避免图片被框架替换成 [Image] 占位符。
- 反选模式：消息不带图片而当前模型支持图片输入时，往后顺延到第一个
  「不支持图片且当日未达限额」的模型，避免多模态模型的额度被纯文本占用。
两者都只往后找，找不到符合条件的模型时继续使用当前模型。

v1.5.0 新增：自定义每日重置时间。全局配置 reset_hour（0-23，默认 0）指定
每天的用量周期起点，小于该小时数的时刻仍计入前一天的周期。
"""

import json
import datetime
import time
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.db.po import ProviderStat
from astrbot.core.provider.entities import LLMResponse, ProviderType
from astrbot.core.star.star_tools import StarTools
from sqlalchemy import func, select
from sqlmodel import col


def _normalize_reset_hour(value) -> int:
    """校验配置的每日重置小时，非法值回退为 0 点。"""
    try:
        hour = int(value)
    except (TypeError, ValueError):
        logger.warning(f"Token路由: reset_hour 配置值 {value!r} 非法，已回退为 0 点重置")
        return 0
    if not 0 <= hour <= 23:
        logger.warning(
            f"Token路由: reset_hour 配置值 {hour} 超出 0-23 范围，已回退为 0 点重置"
        )
        return 0
    return hour


def _resolve_usage_day_start(
    reset_hour: int, now: datetime.datetime | None = None
) -> datetime.datetime:
    """按重置小时计算当前用量周期的起点（本地时区）。

    reset_hour=4 时：03:59 仍属于前一天的周期，04:00 起进入新周期。
    """
    if now is None:
        now = datetime.datetime.now().astimezone()
    start = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
    if now < start:
        start -= datetime.timedelta(days=1)
    return start


@register(
    "astrbot_plugin_token_router",
    "Inoryu7z",
    "按对话窗口追踪token用量，达到每日限额后自动路由到下一个模型，所有模型用尽后回退框架默认模型，每日定时自动重置（重置时间可配置）。支持基于人格的独立路由与多模态跳过。提供 /路由 命令按窗口开关插件介入。",
    "1.5.0",
    "https://github.com/Inoryu7z/-astrbot_plugin_token_router",
)
class TokenRouterPlugin(Star):
    """追踪token用量并在达到限额时路由到下一个模型。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.data_dir = Path(str(StarTools.get_data_dir()))
        self.usage_file = self.data_dir / "usage_data.json"
        self.stats_mode = self.config.get("stats_mode", "window")
        self.debug = bool(self.config.get("debug", False))
        # 每日用量周期的起点小时（本地时间，0-23）。小于该小时的时刻
        # 仍计入前一天的周期，例如填 4 表示每天凌晨 4 点重置。
        self.reset_hour = _normalize_reset_hour(self.config.get("reset_hour", 0))
        # 窗口模式: {umo: {persona_scope: {provider_id: {date, usage}, _exhausted: date}}}
        # persona_scope 为人格ID字符串，空字符串表示未指定人格(兼容旧配置)
        self.token_usage: dict = {}
        # 全局模式: {provider_id: {date, usage}}
        self.global_usage: dict = {}
        # 手动禁用状态: {umo: {persona_scope_key: True}}
        # 被 /路由 命令关闭的 (UMO, 人格) 对，插件暂停介入这些窗口
        self.disabled_windows: dict = {}
        # 存图模型用量追踪: {provider_id: {date, usage}}
        # 供 wardrobe 等插件跨插件调用，按日累计，0点重置
        self.storage_usage: dict = {}
        # DB用量校准（v1.4.0，恒开启）：
        # 框架 OnLLMResponseEvent 每条消息只携带最终一步的 usage，多步 agent 的
        # 中间步（工具调用等）插件钩子看不到，导致钩子桶漏计。每条消息从框架
        # provider_stats 表读取当日各 provider 真实总量（与 WebUI 面板同源），
        # 把「DB总量 - 钩子纯聊天量」的差值按各作用域纯聊天量占比分摊进路由
        # 判定用量，使判定与面板/后端对齐。查询失败自动按无校准处理，不影响运行。
        self._db_cache_ttl = 5.0  # 秒，DB查询节流
        self._db_cache_ts = 0.0  # 上次成功查询的 monotonic 时间戳
        self._db_usage_cache: dict[str, int] = {}  # {provider_id: 当日DB总量}
        self._db_cache_day = ""  # 缓存对应的用量周期日期（YYYY-MM-DD）
        self._load_usage_data()
        logger.info(
            f"Token路由插件已加载，统计模式: {self.stats_mode}，"
            f"调试模式: {'开启' if self.debug else '关闭'}，"
            f"每日重置: {self.reset_hour} 点"
        )

    # ========== 数据持久化 ==========

    def _load_usage_data(self):
        if self.usage_file.exists():
            try:
                with open(self.usage_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.token_usage = data.get("window_usage", {})
                self.global_usage = data.get("global_usage", {})
                self.disabled_windows = data.get("disabled_windows", {})
                self.storage_usage = data.get("storage_usage", {})
                self._migrate_usage_data()
                self._ensure_chat_usage_fields()
            except Exception as e:
                logger.warning(f"Token路由: 加载用量数据失败: {e}")

    def _ensure_chat_usage_fields(self):
        """为旧版用量条目补 chat_usage 字段（v1.4.0 DB校准所需）。

        旧格式 entry 仅含 {date, usage}，无法区分钩子记的聊天量与插件
        上报量。历史数据按「全部为聊天量」近似处理，仅影响升级当日剩余
        时段的校准分摊占比，次日 0 点重置后即为精确口径。
        """
        for data in self.token_usage.values():
            if not isinstance(data, dict):
                continue
            for scope in data.values():
                if not isinstance(scope, dict):
                    continue
                for entry in scope.values():
                    if isinstance(entry, dict) and "chat_usage" not in entry:
                        entry["chat_usage"] = entry.get("usage", 0)
        for entry in self.global_usage.values():
            if isinstance(entry, dict) and "chat_usage" not in entry:
                entry["chat_usage"] = entry.get("usage", 0)

    def _migrate_usage_data(self):
        """将旧版扁平格式迁移到人格感知的嵌套格式。

        旧格式: token_usage[umo][provider_id] = {date, usage}
                token_usage[umo]["_exhausted"] = date
        新格式: token_usage[umo][""][provider_id] = {date, usage}
                token_usage[umo][""]["_exhausted"] = date
        """
        for umo, data in list(self.token_usage.items()):
            if not isinstance(data, dict):
                continue
            # 旧格式特征: 顶层存在 _exhausted 或 provider 条目(含 date/usage)
            is_old = "_exhausted" in data or any(
                isinstance(v, dict) and "date" in v and "usage" in v
                for v in data.values()
            )
            if is_old:
                self.token_usage[umo] = {"": data}
                logger.info(f"Token路由: 已迁移 UMO {umo} 的旧版用量数据到人格嵌套格式")

    def _save_usage_data(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "window_usage": self.token_usage,
                "global_usage": self.global_usage,
                "disabled_windows": self.disabled_windows,
                "storage_usage": self.storage_usage,
            }
            with open(self.usage_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Token路由: 保存用量数据失败: {e}")

    # ========== 日期与重置 ==========

    def _get_usage_day_start(self) -> datetime.datetime:
        """当前用量周期的起点（本地时区），由 reset_hour 决定。"""
        return _resolve_usage_day_start(self.reset_hour)

    def _get_today_str(self) -> str:
        """当前用量周期的标识日期(YYYY-MM-DD)，用作各用量桶的 date 字段。"""
        return self._get_usage_day_start().strftime("%Y-%m-%d")

    def _check_and_reset_daily(self, umo: str, persona_id: str | None, provider_id: str):
        scope = self._peek_window_scope(umo, persona_id)
        if not scope:
            return
        today = self._get_today_str()
        # 清除过期的 _exhausted 标记，避免数据冗余
        exhausted = scope.get("_exhausted")
        if exhausted and exhausted != today:
            scope.pop("_exhausted", None)
        if provider_id in scope:
            entry = scope[provider_id]
            if isinstance(entry, dict) and entry.get("date") != today:
                entry["date"] = today
                entry["usage"] = 0
                entry["chat_usage"] = 0

    def _check_and_reset_global(self, provider_id: str):
        today = self._get_today_str()
        if provider_id in self.global_usage:
            entry = self.global_usage[provider_id]
            if isinstance(entry, dict) and entry.get("date") != today:
                entry["date"] = today
                entry["usage"] = 0
                entry["chat_usage"] = 0

    def _is_all_exhausted(self, umo: str, persona_id: str | None) -> bool:
        today = self._get_today_str()
        scope = self._peek_window_scope(umo, persona_id)
        if scope and scope.get("_exhausted") == today:
            return True
        return False

    def _set_all_exhausted(self, umo: str, persona_id: str | None):
        scope = self._get_window_scope(umo, persona_id)
        scope["_exhausted"] = self._get_today_str()
        self._save_usage_data()

    # ========== 用量记录 ==========

    def _record_usage(
        self,
        umo: str,
        persona_id: str | None,
        provider_id: str,
        tokens: int,
        kind: str = "chat",
    ):
        """记录用量。kind="chat" 为钩子记账（计入 chat_usage，参与校准分摊）；
        kind="plugin" 为跨插件上报（仅计入总用量，不参与校准分摊）。"""
        today = self._get_today_str()
        if self.stats_mode == "global":
            if provider_id not in self.global_usage:
                self.global_usage[provider_id] = {
                    "date": today,
                    "usage": 0,
                    "chat_usage": 0,
                }
            self._check_and_reset_global(provider_id)
            self.global_usage[provider_id]["usage"] += tokens
            if kind == "chat":
                self.global_usage[provider_id]["chat_usage"] += tokens
        else:
            scope = self._get_window_scope(umo, persona_id)
            if provider_id not in scope:
                scope[provider_id] = {
                    "date": today,
                    "usage": 0,
                    "chat_usage": 0,
                }
            self._check_and_reset_daily(umo, persona_id, provider_id)
            scope[provider_id]["usage"] += tokens
            if kind == "chat":
                scope[provider_id]["chat_usage"] += tokens
        self._save_usage_data()

    # ========== DB 用量校准（v1.4.0） ==========

    def _entry_chat_usage(self, entry: dict) -> int:
        """读取条目的纯聊天量（钩子记账部分），旧数据缺字段时按总量兜底。"""
        v = entry.get("chat_usage")
        if v is None:
            return entry.get("usage", 0)
        return v

    async def _refresh_db_usage_cache(self):
        """从框架 provider_stats 表刷新当日各 provider 真实用量（TTL 节流）。

        口径与 WebUI 面板一致（agent_type="internal"，不过滤 status，
        aborted/error 的记录同样计费，按 input_other + input_cached + output
        汇总），但时间起点取**当前用量周期的起点**而非自然日 0 点：配置了
        reset_hour 后两者不同，若仍按 0 点查询，上一周期尾巴的消耗会被当作
        差值补进本周期。DB 的 created_at 是 UTC，需由本地周期起点换算。
        查询失败时清空跨周期旧缓存，校准退化为 0，不影响正常运行。
        """
        now = time.monotonic()
        day = self._get_today_str()
        if now - self._db_cache_ts < self._db_cache_ttl and day == self._db_cache_day:
            return
        try:
            db = self.context.get_db()
            utc_start = self._get_usage_day_start().astimezone(
                datetime.timezone.utc
            )
            async with db.get_db() as session:
                result = await session.execute(
                    select(
                        ProviderStat.provider_id,
                        func.coalesce(func.sum(ProviderStat.token_input_other), 0),
                        func.coalesce(func.sum(ProviderStat.token_input_cached), 0),
                        func.coalesce(func.sum(ProviderStat.token_output), 0),
                    ).where(
                        col(ProviderStat.agent_type) == "internal",
                        col(ProviderStat.created_at) >= utc_start,
                    ).group_by(ProviderStat.provider_id)
                )
                rows = result.all()
            cache: dict[str, int] = {}
            for row in rows:
                provider_id, in_other, in_cached, out = row
                if not provider_id:
                    continue
                cache[provider_id] = (
                    int(in_other or 0) + int(in_cached or 0) + int(out or 0)
                )
            self._db_usage_cache = cache
            self._db_cache_ts = now
            self._db_cache_day = day
            # 高频诊断信息：降为 debug 级，避免 debug 模式下每条消息刷屏
            logger.debug(f"Token路由: DB用量校准数据已刷新: {cache}")
        except Exception as e:
            # 失败时也推进时间戳，避免每条消息都重试打日志
            self._db_cache_ts = now
            if day != self._db_cache_day:
                # 周期已切换但本次查询失败：旧周期缓存不可用，宁可退化为无校准
                self._db_usage_cache = {}
                self._db_cache_day = day
            logger.debug(f"Token路由: DB用量校准查询失败(按无校准处理): {e}")

    def _hook_chat_total(self, provider_id: str) -> int:
        """所有作用域中该 provider 的纯聊天量之和（校准分摊的分母）。"""
        total = 0
        if self.stats_mode == "global":
            entry = self.global_usage.get(provider_id)
            if isinstance(entry, dict):
                total = self._entry_chat_usage(entry)
        else:
            for data in self.token_usage.values():
                if not isinstance(data, dict):
                    continue
                for scope in data.values():
                    if not isinstance(scope, dict):
                        continue
                    entry = scope.get(provider_id)
                    if isinstance(entry, dict):
                        total += self._entry_chat_usage(entry)
        return total

    def _calibration_share(self, provider_id: str, hook_chat: int) -> int:
        """计算当前作用域应分摊的校准量。

        差值 = DB当日总量 - 钩子纯聊天量（钩子只看到每条消息最后一步，
        差值即中间步/中断等被漏掉的部分）。按各作用域纯聊天量占比分摊，
        所有作用域份额之和恰等于差值，保证求和口径不重复、不遗漏。
        """
        if not self._db_usage_cache or self._db_cache_ts <= 0:
            return 0
        # 缓存必须属于当前用量周期（周期切换后未刷新时不得拿旧周期数据校准）
        if self._db_cache_day != self._get_today_str():
            return 0
        db_total = self._db_usage_cache.get(provider_id, 0)
        if db_total <= 0:
            return 0
        hook_total = self._hook_chat_total(provider_id)
        if hook_total <= 0 or hook_chat <= 0:
            return 0
        diff = db_total - hook_total
        if diff <= 0:
            return 0
        return int(diff * hook_chat / hook_total)

    def _get_today_usage(self, umo: str, persona_id: str | None, provider_id: str) -> int:
        """当日用量（含插件上报 + DB 校准分摊）。

        校准逻辑：DB 缓存中有该 provider 的当日总量、且大于钩子纯聊天量时，
        把差值按占比分摊进来，使路由判定与 WebUI 面板/后端口径对齐。
        DB 缓存未刷新（如刚重启、查询失败）时校准量为 0，退化为旧行为。
        """
        if self.stats_mode == "global":
            self._check_and_reset_global(provider_id)
            entry = self.global_usage.get(provider_id)
            if not isinstance(entry, dict):
                return 0
            base = entry.get("usage", 0)
            return base + self._calibration_share(provider_id, self._entry_chat_usage(entry))
        else:
            self._check_and_reset_daily(umo, persona_id, provider_id)
            scope = self._peek_window_scope(umo, persona_id)
            entry = scope.get(provider_id) if scope else None
            if not isinstance(entry, dict):
                return 0
            base = entry.get("usage", 0)
            return base + self._calibration_share(provider_id, self._entry_chat_usage(entry))

    # ========== 窗口作用域辅助 ==========

    def _get_window_scope(self, umo: str, persona_id: str | None) -> dict:
        """获取或创建 (umo, persona_id) 对应的用量作用域。"""
        if umo not in self.token_usage:
            self.token_usage[umo] = {}
        scope_key = persona_id or ""
        if scope_key not in self.token_usage[umo]:
            self.token_usage[umo][scope_key] = {}
        return self.token_usage[umo][scope_key]

    def _peek_window_scope(self, umo: str, persona_id: str | None) -> dict | None:
        """获取 (umo, persona_id) 对应的用量作用域，不创建。"""
        scope_key = persona_id or ""
        return self.token_usage.get(umo, {}).get(scope_key)

    # ========== 手动禁用状态 ==========

    def _is_disabled(self, umo: str, persona_id: str | None) -> bool:
        """检查 (UMO, persona_id) 是否被 /路由 命令手动禁用。"""
        scope_key = persona_id or ""
        return self.disabled_windows.get(umo, {}).get(scope_key, False) is True

    def _toggle_disabled(self, umo: str, persona_id: str | None) -> bool:
        """切换 (UMO, persona_id) 的禁用状态，返回新状态(True=已禁用)。"""
        scope_key = persona_id or ""
        if umo not in self.disabled_windows:
            self.disabled_windows[umo] = {}
        currently_disabled = self.disabled_windows[umo].get(scope_key, False) is True
        if currently_disabled:
            self.disabled_windows[umo].pop(scope_key, None)
            if not self.disabled_windows[umo]:
                self.disabled_windows.pop(umo, None)
            new_state = False
        else:
            self.disabled_windows[umo][scope_key] = True
            new_state = True
        self._save_usage_data()
        return new_state

    # ========== 存图模型用量追踪（供 wardrobe 等插件跨插件调用） ==========

    def _check_and_reset_storage(self, provider_id: str):
        """检查并重置存图模型用量（按日重置）。"""
        today = self._get_today_str()
        if provider_id in self.storage_usage:
            entry = self.storage_usage[provider_id]
            if isinstance(entry, dict) and entry.get("date") != today:
                entry["date"] = today
                entry["usage"] = 0

    def get_storage_usage(self, provider_id: str) -> int:
        """获取存图模型当日用量（token）。"""
        self._check_and_reset_storage(provider_id)
        entry = self.storage_usage.get(provider_id)
        if isinstance(entry, dict):
            return entry.get("usage", 0)
        return 0

    def record_storage_usage(self, provider_id: str, tokens: int):
        """记录存图模型用量。供 wardrobe 在存图分析完成后调用。"""
        if not provider_id or tokens <= 0:
            return
        today = self._get_today_str()
        if provider_id not in self.storage_usage:
            self.storage_usage[provider_id] = {"date": today, "usage": 0}
        self._check_and_reset_storage(provider_id)
        self.storage_usage[provider_id]["usage"] += tokens
        self._save_usage_data()
        if self.debug:
            logger.info(
                f"Token路由[DEBUG]: 存图模型 {provider_id} 用量 "
                f"{self.storage_usage[provider_id]['usage'] - tokens} → "
                f"{self.storage_usage[provider_id]['usage']} (+{tokens})"
            )

    # ========== 插件用量记录（与聊天共享每日额度） ==========

    def _find_plugin_umo_scopes(self, provider_id: str) -> list[tuple[str, str | None]]:
        """查找配置链路中引用了该 provider_id 的所有窗口作用域 (umo, persona_id)。

        供插件上报但无具体 umo 时（如 aiimg 后台定时补拍）归属使用。
        """
        scopes: list[tuple[str, str | None]] = []
        windows_config = self.config.get("windows", {})
        if not isinstance(windows_config, dict):
            return scopes
        for i in range(1, 11):
            window = windows_config.get(f"window_{i}", {})
            if not isinstance(window, dict):
                continue
            umo = window.get("umo", "")
            models = window.get("models", [])
            if not umo or not isinstance(models, list):
                continue
            if any(
                isinstance(m, dict) and m.get("provider_id") == provider_id
                for m in models
            ):
                scopes.append((umo, window.get("persona_id") or None))
        return scopes

    def record_plugin_usage(
        self,
        provider_id: str,
        tokens: int,
        umo: str = "",
        persona_id: str | None = None,
    ):
        """记录插件（如 aiimg 补拍、grok 搜索）产生的 token 用量。

        计入与聊天相同的每日额度桶（window/global），因此插件消耗达到限额时
        聊天会照常顺延（_get_today_usage 会读到合并后的用量）；
        插件自身不参与路由，仍继续使用其指定 provider。

        - stats_mode=global：忽略 umo/persona，按 provider 全局累计
        - stats_mode=window：
            - 提供了 umo：按 (umo, persona) 累计
            - umo 为空（后台任务无事件）：归属到配置链路中**第一个**引用该
              provider 的窗口作用域（v1.4.0 起不再复制到每个匹配窗口，
              修复多窗口引用同一 provider 时的 N 倍重复计数）
        """
        if not provider_id or tokens <= 0:
            return
        if self.stats_mode != "global":
            if not umo:
                scopes = self._find_plugin_umo_scopes(provider_id)
                if scopes:
                    s_umo, s_persona = scopes[0]
                    self._record_usage(s_umo, s_persona, provider_id, tokens, kind="plugin")
                    return
        self._record_usage(umo, persona_id, provider_id, tokens, kind="plugin")

    def get_provider_daily_usage(self, provider_id: str) -> int:
        """获取某 provider 当日总用量（合并所有维度，含 DB 校准分摊）。

        语义：同一 provider 同时作为聊天模型与插件（存图/补拍/搜索）模型时，
        各处消耗共享同一日额度。global 模式返回全局桶用量；
        window 模式返回所有窗口作用域（含空 umo 作用域）中该 provider 的用量之和。
        各作用域的校准份额之和恰等于总差值，求和不会重复计数。
        """
        if not provider_id:
            return 0
        if self.stats_mode == "global":
            return self._get_today_usage("", None, provider_id)
        total = 0
        for umo, data in self.token_usage.items():
            if not isinstance(data, dict):
                continue
            for scope_key in data.keys():
                if scope_key == "_exhausted":
                    continue
                total += self._get_today_usage(umo, scope_key or None, provider_id)
        return total

    def _get_storage_total_usage(self, provider_id: str) -> int:
        """存图路由判定用量 = 存图桶 + 聊天/插件桶（共享日额度）。

        兼容旧调用方：旧版 wardrobe 只上报存图桶、新版上报聊天桶，
        两者来源不同不会重复计数，相加即该 provider 当日总消耗。
        """
        return self.get_storage_usage(provider_id) + self.get_provider_daily_usage(provider_id)

    def get_active_storage_provider(self, providers: list[dict]) -> str:
        """根据当日用量决定存图应使用哪个模型（支持 N 级回退链）。

        逻辑：
        - 按列表顺序找第一个未达限额的 provider（limit<=0 表示不限制）
        - 全部达限额 → 返回列表最后一个（不二次路由，仅日志告警）
        - 用量口径为「存图桶 + 聊天/插件桶」：同一 provider 同时作为
          聊天模型与存图模型时共享日额度，任何一方的消耗都会触发切换

        Args:
            providers: [{"id": "xxx", "daily_limit": 1500000}, ...]
                       按列表顺序组成回退链，至少1个

        Returns:
            应使用的 provider_id
        """
        if not providers:
            return ""

        for i, p in enumerate(providers):
            pid = p.get("id", "")
            limit = int(p.get("daily_limit", 0) or 0)
            if not pid:
                continue
            if limit <= 0:
                return pid
            usage = self._get_storage_total_usage(pid)
            if usage < limit:
                return pid
            logger.info(
                f"Token路由: 存图模型 {pid} 用量 {usage}/{limit}，"
                f"切换到下一个提供商"
            )

        # 全部达限额，返回最后一个
        last = providers[-1].get("id", "")
        last_limit = int(providers[-1].get("daily_limit", 0) or 0)
        last_usage = self._get_storage_total_usage(last) if last else 0
        logger.info(
            f"Token路由: 存图模型全部达限额，继续使用最后一个 {last} "
            f"({last_usage}/{last_limit})，不二次路由"
        )
        return last

    # ========== 配置查找 ==========

    def _find_window_config(self, umo: str, persona_id: str | None, allow_fallback: bool = True) -> dict | None:
        """查找匹配 (UMO, persona_id) 的窗口配置。

        匹配优先级:
        1. UMO + 人格ID 完全匹配(人格ID非空时)
        2. UMO + 空人格ID(通用窗口，对所有人格生效) - 仅当 allow_fallback=True

        allow_fallback=False 时仅匹配显式注册了该人格ID的窗口，
        用于 /路由 命令判断当前人格是否已在插件中注册。
        """
        windows_config = self.config.get("windows", {})
        if not isinstance(windows_config, dict):
            return None

        umo_matches: list[dict] = []
        for i in range(1, 11):
            window = windows_config.get(f"window_{i}", {})
            if isinstance(window, dict) and window.get("umo") == umo:
                umo_matches.append(window)

        if not umo_matches:
            return None

        # 人格ID非空时，优先匹配指定人格的窗口
        if persona_id:
            for window in umo_matches:
                if (window.get("persona_id") or "") == persona_id:
                    return window

        # 回退到通用窗口(未配置人格ID)
        if allow_fallback:
            for window in umo_matches:
                if not (window.get("persona_id") or ""):
                    return window

        return None

    # ========== 路由链解析 ==========

    def _get_active_model_index(self, umo: str, persona_id: str | None, models: list) -> int:
        """获取当前应使用的模型在路由链中的索引。"""
        for i, model in enumerate(models):
            if not isinstance(model, dict):
                continue
            provider_id = model.get("provider_id", "")
            daily_limit = model.get("daily_limit", 200000)
            if not provider_id:
                continue
            today_usage = self._get_today_usage(umo, persona_id, provider_id)
            if today_usage < daily_limit:
                return i
        return -1

    # ========== 多模态顺延（v1.5.0） ==========

    def _provider_supports_image(self, provider_id: str) -> bool:
        """判断 provider 是否支持图片输入。

        口径与框架对齐（astr_main_agent.py:_provider_supports_modality）：
        读取 provider 配置的 modalities 列表，含 "image" 即视为多模态。
        modalities 未配置（None/空列表）或查不到该 provider 配置时视为支持，
        以免插件的模态判断与框架（未配置=支持全部模态）产生分歧。
        """
        if not provider_id:
            return True
        config = None
        try:
            # 优先读实时配置（merged=True 才含 provider_source 层级的 modalities）
            config = self.context.provider_manager.get_provider_config_by_id(
                provider_id, merged=True
            )
        except Exception:
            config = None
        if not isinstance(config, dict):
            # 回退到 provider 实例上的配置（框架自身读 modalities 的方式）
            try:
                provider = self.context.get_provider_by_id(provider_id)
                config = getattr(provider, "provider_config", None)
            except Exception:
                config = None
        if not isinstance(config, dict):
            return True
        modalities = config.get("modalities", None)
        if not isinstance(modalities, list) or not modalities:
            return True
        return "image" in modalities

    def _model_supports_image(self, model) -> bool:
        """判断路由链中的模型条目是否支持图片输入。"""
        if not isinstance(model, dict):
            return True
        return self._provider_supports_image(model.get("provider_id", ""))

    def _message_needs_image(self, event: AstrMessageEvent) -> bool:
        """当前消息是否带图片（含引用消息内的图片）。

        只认 Image 组件，与框架图片兜底切换的口径一致
        （astr_main_agent.py:_select_image_chat_provider 只看 image_urls）。
        """
        try:
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    return True
                if isinstance(comp, Reply):
                    for quoted_comp in getattr(comp, "chain", None) or []:
                        if isinstance(quoted_comp, Image):
                            return True
        except Exception:
            return False
        return False

    def _resolve_modality_index(
        self,
        umo: str,
        persona_id: str | None,
        models: list,
        active_index: int,
        needs_image: bool,
        mode: str,
    ) -> int:
        """按模态规则在路由链上往后找可承接的模型，返回最终索引。

        - forward：消息带图、当前模型不支持图片 → 找之后的第一个
          「支持图片且当日未达限额」的模型
        - reverse：消息不带图、当前模型支持图片 → 找之后的第一个
          「不支持图片且当日未达限额」的模型

        两个模式都只往后找（当前模型之前的模型按定义已用尽额度），
        找不到符合条件的模型时返回原索引，即继续使用当前模型。
        """
        if mode not in ("forward", "reverse"):
            return active_index
        if not models or not 0 <= active_index < len(models):
            return active_index

        current_supports_image = self._model_supports_image(models[active_index])
        if mode == "forward":
            if not needs_image or current_supports_image:
                return active_index
            want_image = True
        else:
            if needs_image or not current_supports_image:
                return active_index
            want_image = False

        for i in range(active_index + 1, len(models)):
            model = models[i]
            if not isinstance(model, dict):
                continue
            provider_id = model.get("provider_id", "")
            if not provider_id:
                continue
            if self._model_supports_image(model) != want_image:
                continue
            daily_limit = model.get("daily_limit", 200000)
            if self._get_today_usage(umo, persona_id, provider_id) < daily_limit:
                return i
        return active_index

    # ========== Provider操作 ==========

    def _get_current_provider_id(self, umo: str) -> str | None:
        try:
            provider = self.context.provider_manager.get_using_provider(
                ProviderType.CHAT_COMPLETION, umo
            )
            if provider:
                return provider.provider_config.get("id")
        except Exception:
            pass
        return None

    # ========== 人格解析 ==========

    async def _get_current_persona_id(self, event: AstrMessageEvent) -> str | None:
        """获取当前事件最终生效的人格ID。

        复用框架 PersonaManager.resolve_selected_persona 的完整解析逻辑:
        UMO级强制人格 > 会话级人格 > 默认人格。
        """
        try:
            umo = event.unified_msg_origin
            conversation_persona_id = None
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
                if conversation:
                    conversation_persona_id = conversation.persona_id

            cfg = self.context.get_config(umo)
            provider_settings = cfg.get("provider_settings", {}) if isinstance(cfg, dict) else {}

            persona_id, _, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conversation_persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=provider_settings,
            )
            # "[%None]" 表示人格被显式禁用
            if persona_id == "[%None]":
                return None
            return persona_id
        except Exception as e:
            logger.warning(f"Token路由: 获取人格ID失败: {e}")
            return None

    # ========== 命令处理 ==========

    @filter.command("路由")
    async def toggle_router(self, event: AstrMessageEvent):
        """切换当前窗口(UMO+人格)的Token路由启用状态。

        仅对已在插件中显式注册了该人格(配置了对应 persona_id 窗口)的窗口生效，
        不回退到通用窗口(空 persona_id)。
        关闭后插件暂停介入该窗口，使用 astrbot 默认链路；再次使用可恢复。
        """
        umo = event.unified_msg_origin
        persona_id = await self._get_current_persona_id(event)

        # 仅允许对已显式注册该人格的窗口使用本命令(不回退到通用窗口)
        window_config = self._find_window_config(umo, persona_id, allow_fallback=False)
        if not window_config:
            persona_desc = persona_id if persona_id else "(未解析到人格)"
            yield event.plain_result(
                f"Token路由: 当前窗口(UMO={umo}, 人格={persona_desc})未在插件中注册专属路由配置，"
                f"无法使用 /路由 命令。请在配置中为该UMO+人格添加窗口后再使用。"
            )
            return

        new_state = self._toggle_disabled(umo, persona_id)
        persona_tag = persona_id if persona_id else "(通用)"
        if new_state:
            yield event.plain_result(
                f"Token路由: 已关闭 UMO={umo} 人格={persona_tag} 的路由介入，"
                f"本插件暂停介入此窗口，将使用astrbot默认链路。"
            )
        else:
            yield event.plain_result(
                f"Token路由: 已开启 UMO={umo} 人格={persona_tag} 的路由介入，"
                f"将按配置的路由链切换模型。"
            )

    # ========== 事件钩子 ==========

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def on_message(self, event: AstrMessageEvent):
        """消息到达时: 通过event.set_extra指定provider，供框架_select_provider读取。

        使用框架原生的selected_provider机制，不干扰其他插件和系统命令。
        只在消息确定要调用LLM时才生效（_select_provider会检查此extra）。

        v1.3.2: 移除is_at_or_wake_command检查。chatplus等插件的"读空气"机制
        会在非@消息上触发LLM调用，此时也需要预先设置selected_provider，
        否则会回退到框架默认provider（可能已暂停），导致503重试和fallback。
        """
        umo = event.unified_msg_origin

        # 快速过滤：UMO不在任何窗口配置中，无需路由（避免对无关消息执行persona解析）
        windows_config = self.config.get("windows", {})
        if isinstance(windows_config, dict):
            umo_in_any = any(
                isinstance(windows_config.get(f"window_{i}", {}), dict)
                and windows_config.get(f"window_{i}", {}).get("umo") == umo
                for i in range(1, 11)
            )
            if not umo_in_any:
                return

        # 跳过已匹配的命令（如 /reset, /help 等）
        handlers_parsed_params = event.get_extra("handlers_parsed_params", {})
        if handlers_parsed_params:
            if self.debug:
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo} 跳过：匹配到指令 {list(handlers_parsed_params.keys())}"
                )
            return

        # 刷新 DB 用量校准缓存（TTL 节流，判定用量前保证数据尽量新鲜）
        await self._refresh_db_usage_cache()

        persona_id = await self._get_current_persona_id(event)
        window_config = self._find_window_config(umo, persona_id)
        if not window_config:
            if self.debug:
                persona_desc = persona_id if persona_id else "(空/未解析)"
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo} 跳过：未匹配到窗口配置(人格={persona_desc})"
                )
            return

        if self._is_disabled(umo, persona_id):
            if self.debug:
                persona_tag = f"/人格 {persona_id}" if persona_id else ""
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo}{persona_tag} 跳过：已被 /路由 命令手动关闭"
                )
            return

        models = window_config.get("models", [])
        if not models:
            if self.debug:
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo} 跳过：窗口未配置模型路由链"
                )
            return

        if self._is_all_exhausted(umo, persona_id):
            if self.debug:
                persona_tag = f"/人格 {persona_id}" if persona_id else ""
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo}{persona_tag} 跳过：所有模型今日已用尽"
                )
            return

        active_index = self._get_active_model_index(umo, persona_id, models)
        if active_index == -1:
            if self.debug:
                persona_tag = f"/人格 {persona_id}" if persona_id else ""
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo}{persona_tag} 跳过：无可用模型(active_index=-1)"
                )
            return

        # 多模态顺延（v1.5.0）：按窗口配置的模态规则调整本次使用的模型。
        # 本阶段不发日志，路由决策存入 event extra，由 on_llm_response
        # 在回复完成后输出单条汇总日志（避免一条消息多条 DEBUG 刷屏）。
        modality_mode = window_config.get("modality_skip", "off")
        quota_provider_id = models[active_index].get("provider_id", "")
        if modality_mode in ("forward", "reverse"):
            needs_image = self._message_needs_image(event)
            active_index = self._resolve_modality_index(
                umo, persona_id, models, active_index, needs_image, modality_mode
            )

        active_model = models[active_index]
        target_provider_id = active_model.get("provider_id", "")
        if not target_provider_id:
            if self.debug:
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo} 跳过：模型#{active_index}未配置provider_id"
                )
            return

        # 通过框架原生机制指定provider
        event.set_extra("selected_provider", target_provider_id)
        # 路由决策信息（额度路由选出的模型 + 模态顺延模式），供汇总日志展示
        event.set_extra(
            "token_router_route",
            {
                "from": quota_provider_id,
                "mode": modality_mode if modality_mode in ("forward", "reverse") else "",
            },
        )

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM响应后: 记录token用量，标记耗尽状态。

        注意：不调用 set_provider() 改变会话 provider。
        路由逻辑完全由 on_message 中的 selected_provider 机制处理，
        每条消息独立决定使用的 provider，不与系统指令/其他插件冲突。
        """
        umo = event.unified_msg_origin
        # 刷新 DB 用量校准缓存（TTL 节流），供记账后的限额判定使用
        await self._refresh_db_usage_cache()
        persona_id = await self._get_current_persona_id(event)
        window_config = self._find_window_config(umo, persona_id)
        if not window_config:
            if self.debug:
                persona_desc = persona_id if persona_id else "(空/未解析)"
                logger.info(
                    f"Token路由[DEBUG]: on_llm_response UMO {umo} 跳过：未匹配到窗口配置(人格={persona_desc})"
                )
            return

        if self._is_disabled(umo, persona_id):
            return

        if self._is_all_exhausted(umo, persona_id):
            return

        # 优先从event extra获取本次实际使用的provider
        provider_id = event.get_extra("selected_provider")
        if not provider_id:
            provider_id = self._get_current_provider_id(umo)
        if not provider_id:
            if self.debug:
                persona_tag = f"/人格 {persona_id}" if persona_id else ""
                logger.info(
                    f"Token路由[DEBUG]: on_llm_response UMO {umo}{persona_tag} 跳过：无法获取provider_id(selected_provider为空且会话provider解析失败)"
                )
            return

        # 查找当前provider在配置中的位置
        models = window_config.get("models", [])
        current_index = -1
        for i, model in enumerate(models):
            if isinstance(model, dict) and model.get("provider_id") == provider_id:
                current_index = i
                break

        # provider 不在路由链中时不记录用量，避免数据冗余
        if current_index == -1:
            if self.debug:
                persona_tag = f"/人格 {persona_id}" if persona_id else ""
                configured = [m.get("provider_id") for m in models if isinstance(m, dict)]
                logger.info(
                    f"Token路由[DEBUG]: on_llm_response UMO {umo}{persona_tag} 跳过：provider {provider_id} 不在路由链中(已配置: {configured})"
                )
            return

        # 记录token用量
        if resp.usage:
            usage = resp.usage.total
            before = self._get_today_usage(umo, persona_id, provider_id)
            self._record_usage(umo, persona_id, provider_id, usage)
            if self.debug:
                after = self._get_today_usage(umo, persona_id, provider_id)
                persona_tag = f"/人格 {persona_id}" if persona_id else ""
                scope_tag = "(全局)" if self.stats_mode == "global" else ""
                # 单条汇总：模型 + 多模态顺延来源 + 用量变动
                route_info = event.get_extra("token_router_route")
                if not isinstance(route_info, dict):
                    route_info = {}
                model_part = provider_id
                if (
                    route_info.get("mode") in ("forward", "reverse")
                    and route_info.get("from")
                    and route_info["from"] != provider_id
                ):
                    mode_name = (
                        "正向顺延" if route_info["mode"] == "forward" else "反选模式"
                    )
                    model_part = f"{route_info['from']} → {provider_id}({mode_name})"
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo}{persona_tag} {model_part} "
                    f"用量 {before} → {after} (+{usage}){scope_tag}"
                )

        # 检查是否达到限额
        current_model = models[current_index]
        daily_limit = current_model.get("daily_limit", 200000)
        today_usage = self._get_today_usage(umo, persona_id, provider_id)

        if today_usage >= daily_limit:
            # 查找下一个未达限额的模型
            next_index = -1
            for i in range(current_index + 1, len(models)):
                next_model = models[i]
                if not isinstance(next_model, dict):
                    continue
                next_pid = next_model.get("provider_id", "")
                next_limit = next_model.get("daily_limit", 200000)
                if next_pid and self._get_today_usage(umo, persona_id, next_pid) < next_limit:
                    next_index = i
                    break

            if next_index != -1:
                next_provider_id = models[next_index].get("provider_id")
                if next_provider_id:
                    # 不调用 set_provider，由下次 on_message 的 selected_provider 机制接管
                    persona_tag = f"/人格 {persona_id}" if persona_id else ""
                    logger.info(
                        f"Token路由: UMO {umo}{persona_tag} 的模型 "
                        f"{provider_id} 用量 {today_usage}/{daily_limit}"
                        f"{'(全局)' if self.stats_mode == 'global' else ''}，"
                        f"下次请求将自动切换到 {next_provider_id}"
                    )
            else:
                # 所有模型已用尽，标记为耗尽状态
                self._set_all_exhausted(umo, persona_id)
                persona_tag = f"/人格 {persona_id}" if persona_id else ""
                logger.info(
                    f"Token路由: UMO {umo}{persona_tag} 的所有模型已用尽，"
                    f"后续请求将回退到框架默认模型"
                )

    async def terminate(self):
        self._save_usage_data()
        logger.info("Token路由插件已卸载")
