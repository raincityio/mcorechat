#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Optional, Protocol, Union

from meshcore.events import Event

from mesh2irc.common import ContactName, ChannelName, Message, MessageId

Destination = Union[ContactName, ChannelName]
# async def callback(source: ContactName, destination: Destination, message: Message, message_id: MessageId) -> None
ChannelCallback = Callable[[ContactName, Destination, Message, MessageId], Awaitable[None]]


class Chatter(Protocol):

    async def send_message(
        self, source: ContactName, message: Message, event: Event, *, channel_name: Optional[ChannelName] = None
    ) -> None: ...

    async def add_message_callback(self, cb: ChannelCallback) -> None: ...

    async def remove_message_callback(self, cb: ChannelCallback) -> None: ...


__all__ = ["Destination", "ChannelCallback", "Chatter"]
