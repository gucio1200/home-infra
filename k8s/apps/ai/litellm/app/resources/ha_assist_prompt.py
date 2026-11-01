import re

from litellm.integrations.custom_logger import CustomLogger

MODEL_GROUP = "ha-assist"
DATE_TIME = re.compile(r"Current time is [0-9:]+\. Today's date is [0-9-]+\.\n?")


class HaAssistPrompt(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if data.get("model") != MODEL_GROUP:
            return data
        messages = data.get("messages") or []
        system = next((m for m in messages if m.get("role") == "system"), None)
        user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if system is None or user is None or not isinstance(system.get("content"), str):
            return data
        match = DATE_TIME.search(system["content"])
        if match is None:
            return data
        system["content"] = system["content"].replace(match.group(0), "", 1)
        stamp = match.group(0).strip()
        content = user.get("content")
        if isinstance(content, str):
            user["content"] = f"{stamp}\n{content}"
        elif isinstance(content, list):
            user["content"] = [{"type": "text", "text": stamp}, *content]
        return data


proxy_handler_instance = HaAssistPrompt()
