import hashlib
import json

from litellm.integrations.custom_logger import CustomLogger

SESSION_HEADER = "x-opencode-session"
CLIENT_SESSION_HEADERS = (
    "x-opencode-session",
    "x-claude-code-session-id",
    "x-openwebui-chat-id",
    "x-session-id",
)
OPENCODE_API_BASE = "opencode.ai"
USER_AGENT = "home-litellm/1.0"


def is_opencode_group(model):
    from litellm.proxy.proxy_server import llm_router

    if not model or llm_router is None:
        return False
    return any(
        OPENCODE_API_BASE in str((d.get("litellm_params") or {}).get("api_base") or "")
        for d in llm_router.get_model_list(model_name=model) or []
    )


def fallback_groups(model):
    from litellm.proxy.proxy_server import llm_router

    if not model or llm_router is None:
        return []
    return [group for entry in llm_router.fallbacks or [] for group in entry.get(model) or []]


def first_user_content(data):
    for message in data.get("messages") or []:
        if message.get("role") == "user":
            return message.get("content")
    return data.get("input")


class OpenCodeSession(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        model = data.get("model")
        if not any(is_opencode_group(group) for group in [model, *fallback_groups(model)]):
            return data
        request = data.get("proxy_server_request") or {}
        incoming = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
        session = next((incoming[h] for h in CLIENT_SESSION_HEADERS if incoming.get(h)), None)
        if not session:
            seed = json.dumps(
                [user_api_key_dict.api_key, incoming.get("user-agent"), first_user_content(data)],
                sort_keys=True,
                default=str,
            )
            session = "litellm-" + hashlib.sha256(seed.encode()).hexdigest()[:32]
        data["extra_headers"] = {
            **(data.get("extra_headers") or {}),
            SESSION_HEADER: session,
            "User-Agent": USER_AGENT,
        }
        return data


proxy_handler_instance = OpenCodeSession()
