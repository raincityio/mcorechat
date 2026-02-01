#!/usr/bin/env python3
import dataclasses
from collections.abc import Callable, Awaitable
from typing import Optional, Protocol, Union, NewType

from meshcore.events import Event

Message = NewType("Message", str)
MessageId = NewType("MessageId", str)
# ChannelName = NewType("ChannelName", str)
# UserName = NewType("UserName", str)


# @dataclasses.dataclass(frozen=True)
# class Message:
#     content: str
#     id: str


@dataclasses.dataclass(frozen=True)
class ChannelName:
    raw: str

    def __str__(self) -> str:
        return self.raw


@dataclasses.dataclass(frozen=True)
class UserName:
    raw: str

    def __str__(self) -> str:
        return self.raw


Destination = Union[UserName, ChannelName]
ChannelCallback = Callable[[UserName, Destination, Message, MessageId], Awaitable[None]]


class Chatter(Protocol):

    async def send_message(
        self, source: UserName, message: Message, event: Event, *, channel_name: Optional[ChannelName] = None
    ) -> None: ...

    async def add_message_callback(self, cb: ChannelCallback) -> None: ...

    async def remove_message_callback(self, cb: ChannelCallback) -> None: ...
