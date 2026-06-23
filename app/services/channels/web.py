"""Веб-чат — первый канал (свой виджет, WebSocket). Не зависит от внешних API."""
from app.services.channels.base import ChannelAdapter, NormalizedMessage


class WebChatAdapter(ChannelAdapter):
    type = "web"

    def parse_inbound(self, payload: dict) -> NormalizedMessage:
        raise NotImplementedError  # TODO

    async def send_outbound(self, conversation_ref: str, text: str) -> None:
        raise NotImplementedError  # TODO: push в WebSocket
