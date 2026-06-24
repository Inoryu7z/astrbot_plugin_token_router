"""
astrbot_plugin_token_router - Token用量追踪与模型路由插件

追踪每个对话窗口(UMO)的token用量，当某个模型的每日用量达到限额时，
自动切换到路由链中的下一个模型。当所有模型都达到限额时，回退到框架默认模型。
每天0点(本地时间)自动重置用量计数。
"""

import json
import datetime
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest, LLMResponse, ProviderType
from astrbot.core.star.star_tools import StarTools


@register(
    "astrbot_plugin_token_router",
    "Inoryu7z",
    "按对话窗口追踪token用量，达到每日限额后自动路由到下一个模型，所有模型用尽后回退框架默认模型，每天0点自动重置。",
    "1.0.0",
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
        # 数据结构:
        # {
        #   "umo_string": {
        #     "provider_id_1": {"date": "2026-06-24", "usage": 150000},
        #     "provider_id_2": {"date": "2026-06-24", "usage": 50000},
        #     "_exhausted": "2026-06-24"
        #   }
        # }
        self.token_usage: dict = self._load_usage_data()
        logger.info("Token路由插件已加载")

    # ========== 数据持久化 ==========

    def _load_usage_data(self) -> dict:
        """从文件加载token用量数据。"""
        if self.usage_file.exists():
            try:
                with open(self.usage_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Token路由: 加载用量数据失败: {e}")
        return {}

    def _save_usage_data(self):
        """保存token用量数据到文件。"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.usage_file, "w", encoding="utf-8") as f:
                json.dump(self.token_usage, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Token路由: 保存用量数据失败: {e}")

    # ========== 日期与重置 ==========

    def _get_today_str(self) -> str:
        """获取今天的日期字符串 (YYYY-MM-DD)。"""
        return datetime.datetime.now().strftime("%Y-%m-%d")

    def _check_and_reset_daily(self, umo: str, provider_id: str):
        """检查日期是否变更，如果变更则重置该provider的用量。"""
        today = self._get_today_str()
        if umo in self.token_usage and provider_id in self.token_usage[umo]:
            entry = self.token_usage[umo][provider_id]
            if isinstance(entry, dict) and entry.get("date") != today:
                entry["date"] = today
                entry["usage"] = 0

    def _is_all_exhausted(self, umo: str) -> bool:
        """检查某UMO的所有模型今天是否已用尽。"""
        today = self._get_today_str()
        if umo in self.token_usage:
            exhausted_date = self.token_usage[umo].get("_exhausted")
            if exhausted_date == today:
                return True
        return False

    def _set_all_exhausted(self, umo: str):
        """标记某UMO的所有模型今天已用尽。"""
        today = self._get_today_str()
        if umo not in self.token_usage:
            self.token_usage[umo] = {}
        self.token_usage[umo]["_exhausted"] = today
        self._save_usage_data()

    # ========== 用量记录 ==========

    def _record_usage(self, umo: str, provider_id: str, tokens: int):
        """记录token用量。"""
        today = self._get_today_str()
        if umo not in self.token_usage:
            self.token_usage[umo] = {}
        if provider_id not in self.token_usage[umo]:
            self.token_usage[umo][provider_id] = {"date": today, "usage": 0}
        self._check_and_reset_daily(umo, provider_id)
        self.token_usage[umo][provider_id]["usage"] += tokens
        self._save_usage_data()

    def _get_today_usage(self, umo: str, provider_id: str) -> int:
        """获取某UMO某provider今天的token用量。"""
        self._check_and_reset_daily(umo, provider_id)
        if umo in self.token_usage and provider_id in self.token_usage[umo]:
            entry = self.token_usage[umo][provider_id]
            if isinstance(entry, dict):
                return entry.get("usage", 0)
        return 0

    # ========== 配置查找 ==========

    def _find_window_config(self, umo: str) -> dict | None:
        """根据UMO查找窗口配置。"""
        windows = self.config.get("windows", [])
        if not windows:
            return None
        for window in windows:
            if isinstance(window, dict) and window.get("umo") == umo:
                return window
        return None

    # ========== Provider操作 ==========

    def _get_current_provider_id(self, umo: str) -> str | None:
        """获取某UMO当前使用的provider ID。"""
        try:
            provider = self.context.provider_manager.get_using_provider(
                ProviderType.CHAT_COMPLETION, umo
            )
            if provider:
                return provider.provider_config.get("id")
        except Exception as e:
            logger.warning(f"Token路由: 获取当前provider失败: {e}")
        return None

    def _get_default_provider_id(self) -> str | None:
        """获取框架默认的provider ID。"""
        try:
            provider = self.context.provider_manager.get_using_provider(
                ProviderType.CHAT_COMPLETION, None
            )
            if provider:
                return provider.provider_config.get("id")
        except Exception as e:
            logger.warning(f"Token路由: 获取默认provider失败: {e}")
        return None

    # ========== 事件钩子 ==========

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM请求前: 设置配置中指定的模型名。"""
        umo = event.unified_msg_origin
        window_config = self._find_window_config(umo)
        if not window_config:
            return

        provider_id = self._get_current_provider_id(umo)
        if not provider_id:
            return

        models = window_config.get("models", [])
        for model in models:
            if isinstance(model, dict) and model.get("provider_id") == provider_id:
                model_name = model.get("model_name", "")
                if model_name:
                    req.model = model_name
                break

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM响应后: 记录token用量，达到限额时切换模型。"""
        umo = event.unified_msg_origin
        window_config = self._find_window_config(umo)
        if not window_config:
            return

        # 今天所有模型已用尽，不再处理
        if self._is_all_exhausted(umo):
            return

        provider_id = self._get_current_provider_id(umo)
        if not provider_id:
            return

        # 记录token用量
        if resp.usage:
            usage = resp.usage.total
            self._record_usage(umo, provider_id, usage)

        # 查找当前provider在配置中的位置
        models = window_config.get("models", [])
        current_index = -1
        for i, model in enumerate(models):
            if isinstance(model, dict) and model.get("provider_id") == provider_id:
                current_index = i
                break

        if current_index == -1:
            return  # 当前provider不在配置中，不处理

        # 检查是否达到限额
        current_model = models[current_index]
        daily_limit = current_model.get("daily_limit", 200000)
        today_usage = self._get_today_usage(umo, provider_id)

        if today_usage >= daily_limit:
            if current_index + 1 < len(models):
                # 切换到下一个模型
                next_model = models[current_index + 1]
                next_provider_id = next_model.get("provider_id")
                if next_provider_id:
                    try:
                        await self.context.provider_manager.set_provider(
                            next_provider_id,
                            ProviderType.CHAT_COMPLETION,
                            umo,
                        )
                        logger.info(
                            f"Token路由: UMO {umo} 的模型 "
                            f"{provider_id} 用量 {today_usage}/{daily_limit}，"
                            f"已切换到 {next_provider_id}"
                        )
                    except Exception as e:
                        logger.warning(f"Token路由: 切换到下一个模型失败: {e}")
            else:
                # 所有模型已用尽，回退到框架默认模型
                self._set_all_exhausted(umo)
                default_provider_id = self._get_default_provider_id()
                if default_provider_id and default_provider_id != provider_id:
                    try:
                        await self.context.provider_manager.set_provider(
                            default_provider_id,
                            ProviderType.CHAT_COMPLETION,
                            umo,
                        )
                        logger.info(
                            f"Token路由: UMO {umo} 的所有模型已用尽，"
                            f"回退到框架默认模型 {default_provider_id}"
                        )
                    except Exception as e:
                        logger.warning(f"Token路由: 回退到默认模型失败: {e}")
                else:
                    logger.info(
                        f"Token路由: UMO {umo} 的所有模型已用尽，"
                        f"默认模型与当前相同，保持不变"
                    )

    async def terminate(self):
        """插件卸载时保存数据。"""
        self._save_usage_data()
        logger.info("Token路由插件已卸载")
