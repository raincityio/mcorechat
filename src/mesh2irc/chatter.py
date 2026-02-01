#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Optional, Protocol, Union

from meshcore.events import Event

from mesh2irc.common import UserName, ChannelName, Message, MessageId

Destination = Union[UserName, ChannelName]
ChannelCallback = Callable[[UserName, Destination, Message, MessageId], Awaitable[None]]


class Chatter(Protocol):

    async def send_message(
        self, source: UserName, message: Message, event: Event, *, channel_name: Optional[ChannelName] = None
    ) -> None: ...

    async def add_message_callback(self, cb: ChannelCallback) -> None: ...

    async def remove_message_callback(self, cb: ChannelCallback) -> None: ...


__all__ = ["Destination", "ChannelCallback", "Chatter"]
