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
"""

import json
import datetime
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import LLMResponse, ProviderType
from astrbot.core.star.star_tools import StarTools


@register(
    "astrbot_plugin_token_router",
    "Inoryu7z",
    "按对话窗口追踪token用量，达到每日限额后自动路由到下一个模型，所有模型用尽后回退框架默认模型，每天0点自动重置。支持基于人格的独立路由。",
    "1.3.2",
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
        # 窗口模式: {umo: {persona_scope: {provider_id: {date, usage}, _exhausted: date}}}
        # persona_scope 为人格ID字符串，空字符串表示未指定人格(兼容旧配置)
        self.token_usage: dict = {}
        # 全局模式: {provider_id: {date, usage}}
        self.global_usage: dict = {}
        self._load_usage_data()
        logger.info(f"Token路由插件已加载，统计模式: {self.stats_mode}，调试模式: {'开启' if self.debug else '关闭'}")

    # ========== 数据持久化 ==========

    def _load_usage_data(self):
        if self.usage_file.exists():
            try:
                with open(self.usage_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.token_usage = data.get("window_usage", {})
                self.global_usage = data.get("global_usage", {})
                self._migrate_usage_data()
            except Exception as e:
                logger.warning(f"Token路由: 加载用量数据失败: {e}")

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
            }
            with open(self.usage_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Token路由: 保存用量数据失败: {e}")

    # ========== 日期与重置 ==========

    def _get_today_str(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d")

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

    def _check_and_reset_global(self, provider_id: str):
        today = self._get_today_str()
        if provider_id in self.global_usage:
            entry = self.global_usage[provider_id]
            if isinstance(entry, dict) and entry.get("date") != today:
                entry["date"] = today
                entry["usage"] = 0

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

    def _record_usage(self, umo: str, persona_id: str | None, provider_id: str, tokens: int):
        today = self._get_today_str()
        if self.stats_mode == "global":
            if provider_id not in self.global_usage:
                self.global_usage[provider_id] = {"date": today, "usage": 0}
            self._check_and_reset_global(provider_id)
            self.global_usage[provider_id]["usage"] += tokens
        else:
            scope = self._get_window_scope(umo, persona_id)
            if provider_id not in scope:
                scope[provider_id] = {"date": today, "usage": 0}
            self._check_and_reset_daily(umo, persona_id, provider_id)
            scope[provider_id]["usage"] += tokens
        self._save_usage_data()

    def _get_today_usage(self, umo: str, persona_id: str | None, provider_id: str) -> int:
        if self.stats_mode == "global":
            self._check_and_reset_global(provider_id)
            if provider_id in self.global_usage:
                entry = self.global_usage[provider_id]
                if isinstance(entry, dict):
                    return entry.get("usage", 0)
            return 0
        else:
            self._check_and_reset_daily(umo, persona_id, provider_id)
            scope = self._peek_window_scope(umo, persona_id)
            if scope and provider_id in scope:
                entry = scope[provider_id]
                if isinstance(entry, dict):
                    return entry.get("usage", 0)
            return 0

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

    # ========== 配置查找 ==========

    def _find_window_config(self, umo: str, persona_id: str | None) -> dict | None:
        """查找匹配 (UMO, persona_id) 的窗口配置。

        匹配优先级:
        1. UMO + 人格ID 完全匹配(人格ID非空时)
        2. UMO + 空人格ID(通用窗口，对所有人格生效)
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

        persona_id = await self._get_current_persona_id(event)
        window_config = self._find_window_config(umo, persona_id)
        if not window_config:
            if self.debug:
                persona_desc = persona_id if persona_id else "(空/未解析)"
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo} 跳过：未匹配到窗口配置(人格={persona_desc})"
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

        if self.debug:
            persona_tag = f"/人格 {persona_id}" if persona_id else ""
            logger.info(
                f"Token路由[DEBUG]: UMO {umo}{persona_tag} 本次使用模型 {target_provider_id}"
            )

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM响应后: 记录token用量，标记耗尽状态。

        注意：不调用 set_provider() 改变会话 provider。
        路由逻辑完全由 on_message 中的 selected_provider 机制处理，
        每条消息独立决定使用的 provider，不与系统指令/其他插件冲突。
        """
        umo = event.unified_msg_origin
        persona_id = await self._get_current_persona_id(event)
        window_config = self._find_window_config(umo, persona_id)
        if not window_config:
            if self.debug:
                persona_desc = persona_id if persona_id else "(空/未解析)"
                logger.info(
                    f"Token路由[DEBUG]: on_llm_response UMO {umo} 跳过：未匹配到窗口配置(人格={persona_desc})"
                )
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
                logger.info(
                    f"Token路由[DEBUG]: UMO {umo}{persona_tag} 模型 {provider_id} "
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
                    if self.debug:
                        logger.info(
                            f"Token路由[DEBUG]: UMO {umo}{persona_tag} 模型切换 "
                            f"{provider_id} → {next_provider_id}"
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
