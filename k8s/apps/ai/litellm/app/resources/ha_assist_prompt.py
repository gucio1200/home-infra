import re

from litellm.integrations.custom_logger import CustomLogger

MODEL_GROUP = "ha-assist"
DATE_TIME = re.compile(r"Current time is [0-9:]+\. Today's date is [0-9-]+\.\n?")
KEEP_USER_TURNS = 2


def trim_history(messages):
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(users) <= KEEP_USER_TURNS:
        return messages
    start = users[-KEEP_USER_TURNS]
    return [m for m in messages[:start] if m.get("role") == "system"] + messages[start:]


def move_date_time(messages):
    system = next((m for m in messages if m.get("role") == "system"), None)
    user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if system is None or user is None or not isinstance(system.get("content"), str):
        return
    match = DATE_TIME.search(system["content"])
    if match is None:
        return
    system["content"] = system["content"].replace(match.group(0), "", 1)
    stamp = match.group(0).strip()
    content = user.get("content")
    if isinstance(content, str):
        user["content"] = f"{stamp}\n{content}"
    elif isinstance(content, list):
        user["content"] = [{"type": "text", "text": stamp}, *content]


class HaAssistPrompt(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if data.get("model") != MODEL_GROUP:
            return data
        messages = trim_history(data.get("messages") or [])
        move_date_time(messages)
        data["messages"] = messages
        return data


proxy_handler_instance = HaAssistPrompt()
