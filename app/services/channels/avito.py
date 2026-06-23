"""Avito Messenger API + вебхуки. Ядро ICP. TODO: OAuth-подключение, лимиты."""
from app.services.channels.base import ChannelAdapter, NormalizedMessage


class AvitoAdapter(ChannelAdapter):
    type = "avito"

    def parse_inbound(self, payload: dict) -> NormalizedMessage:
        raise NotImplementedError

    async def send_outbound(self, conversation_ref: str, text: str) -> None:
        raise NotImplementedError
