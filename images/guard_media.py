"""图片模型选择：自动按能力筛选，手选具体对话模型允许尝试。"""
import re

try:
    from ..gate.guard_engine import GateFailure, GateProvider
    from ..integrations.guard_router_connections import CHAT_TYPES
except ImportError:
    from gate.guard_engine import GateFailure, GateProvider
    from integrations.guard_router_connections import CHAT_TYPES


VISION_ROUTER_API_TYPES = frozenset({"image_to_text", "multimodal"})


def supports_images(provider, model="", *, api_type=None):
    # 仅路由上游传入 api_type：显式能力优先，原生 Provider 保留原有判断。
    # 路由构造的 client 没有 modalities，不能因此把 HY4 等新型号拒绝掉。
    if api_type is not None:
        return str(api_type).strip().lower() in VISION_ROUTER_API_TYPES
    config = getattr(provider, "provider_config", {}) or {}
    getter = getattr(provider, "get_model", None)
    name = (model or (getter() if callable(getter) else "") or "").lower()
    # 已知纯文本型号不因宿主默认勾选全部modalities变成视觉模型。
    if (("deepseek" in name and not re.search(r"deepseek[-/]vl", name))
            or ("qwen" in name and "vl" not in name)
            or "hunyuanmt" in name or "embedding" in name):
        return False
    modalities = config.get("modalities")
    if isinstance(modalities, list) and modalities:
        # 宿主显式关闭图片时，不能仅靠模型名称覆盖该设置。
        return "image" in [str(value).lower() for value in modalities]
    explicit = config.get("supports_vision")
    if isinstance(explicit, bool):
        return explicit
    # 没有能力字段时只认明确视觉型号，不能把全部主流品牌都当成多模态。
    return bool(re.search(
        r"qwen[^/]*(?:-vl|2\.5-vl)|gemini|claude-(?:3|sonnet-4|opus-4|haiku-4)"
        r"|gpt-4o|gpt-4\.1|gpt-5(?!.*(?:codex|text))|glm-[^-]*v|internvl|pixtral"
        r"|vision|deepseek-vl", name))


class StrictVisionProvider:
    """OpenAI宿主默认会在视觉报错后删图重试，闸门必须把该异常交给下一层。"""

    def __init__(self, provider):
        self.provider = provider

    async def text_chat(self, **kwargs):
        payload, _ = await self.provider._prepare_chat_payload(
            kwargs["prompt"], kwargs.get("image_urls"), None, [],
            kwargs["system_prompt"], None, model=kwargs.get("model"),
        )
        config = getattr(self.provider, "provider_config", {}) or {}
        limit_key = ("max_output_tokens" if config.get("type") == "openai_responses"
                     else "max_tokens")
        payload[limit_key] = kwargs.get("max_tokens", 900)
        return await self.provider._query(
            payload, None, request_max_retries=kwargs.get("request_max_retries", 1))


def _strict_provider(provider, *, include_responses=False):
    config = getattr(provider, "provider_config", {}) or {}
    kind = config.get("type")
    if ((kind == "openai_chat_completion" or (include_responses and kind == "openai_responses"))
            and callable(getattr(provider, "_prepare_chat_payload", None))
            and callable(getattr(provider, "_query", None))):
        return StrictVisionProvider(provider)
    return provider


def image_target(provider, label="", *, api_type=None, allow_unconfirmed=False):
    if provider is None:
        raise GateFailure("no_vision_provider")
    config = getattr(provider, "provider_config", {}) or {}
    provider_id = str(config.get("id", "") or type(provider).__name__)
    pinned = label.split("::", 1)[1].strip() if "::" in label else ""
    manual = bool(allow_unconfirmed and label)
    failure = "no_vision_provider" if label else "main_no_vision"
    if manual and (config.get("enable") is False or not callable(getattr(provider, "text_chat", None))):
        raise GateFailure(failure)
    upstreams = getattr(provider, "upstreams", None)
    if isinstance(upstreams, (list, tuple)):
        # 不把图片交给未锁定的混合路由，绕开“找不到pin就走全部上游”的行为。
        lock = config.get("upstream_slot")
        choices = []
        unconfirmed = None
        model_counts = {}
        for upstream in upstreams:
            client = getattr(upstream, "client", None)
            if client is None:
                continue
            model = str(client.get_model() or "")
            if not model or (pinned and pinned != model):
                continue
            declaration = getattr(upstream, "api_type", None)
            model_counts[model] = model_counts.get(model, 0) + 1
            if supports_images(client, model, api_type=declaration):
                choices.append((model, client))
            elif (manual and unconfirmed is None
                  and (pinned or (lock and len(upstreams) == 1))
                  and (declaration is None or str(declaration).strip().lower() in CHAT_TYPES)):
                unconfirmed = (model, client)
        if choices:
            model, client = choices[0]
            # 选了自动路由或旧模型别名时，同名上游不等于同一条线路。
            if manual and model_counts[model] > 1:
                provider = _strict_provider(client, include_responses=True)
        elif unconfirmed:
            model, client = unconfirmed
            # 手选无能力声明的上游只发给这一条线，避免路由重选或宿主删图重试。
            provider = _strict_provider(client, include_responses=True)
        else:
            raise GateFailure(failure)
    else:
        configured = str(provider.get_model() or "")
        model = pinned or configured
        # 存量的原生 provider::model 仍可按能力走；手选放开仅针对已配置的型号。
        try_configured = manual and (not pinned or pinned == configured)
        if not supports_images(provider, model, api_type=api_type) and not try_configured:
            raise GateFailure(failure)
        provider = _strict_provider(provider, include_responses=manual)
    return GateProvider(provider, model, f"{provider_id}::{model}")
