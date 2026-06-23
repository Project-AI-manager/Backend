"""VK Callback / Long Poll API (сообщения сообщества). TODO."""
from app.services.channels.base import ChannelAdapter, NormalizedMessage


class VkAdapter(ChannelAdapter):
    type = "vk"

    def parse_inbound(self, payload: dict) -> NormalizedMessage:
        raise NotImplementedError

    async def send_outbound(self, conversation_ref: str, text: str) -> None:
        raise NotImplementedError
