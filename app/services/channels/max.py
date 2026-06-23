"""MAX (мессенджер) — за пределами MVP (зрелость Bot API под вопросом). Заглушка-задел."""
from app.services.channels.base import ChannelAdapter, NormalizedMessage


class MaxAdapter(ChannelAdapter):
    type = "max"

    def parse_inbound(self, payload: dict) -> NormalizedMessage:
        raise NotImplementedError

    async def send_outbound(self, conversation_ref: str, text: str) -> None:
        raise NotImplementedError
