"""Единый интерфейс канального адаптера. Каналы приводятся к одной модели сообщения.

См. wiki/concepts/channel-integrations.md.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class NormalizedMessage:
    channel: str
    external_conversation_id: str
    external_message_id: str
    customer_ref: str
    text: str
    customer_name: str = ""
    attachments: dict = field(default_factory=dict)


class ChannelAdapter(ABC):
    type: str

    @abstractmethod
    def parse_inbound(self, payload: dict) -> NormalizedMessage: ...

    @abstractmethod
    async def send_outbound(self, conversation_ref: str, text: str) -> None: ...

    def verify_signature(self, payload: dict, headers: dict) -> bool:
        return True  # TODO: проверка подписи вебхука
