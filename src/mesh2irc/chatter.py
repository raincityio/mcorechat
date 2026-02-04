#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Protocol, Union

from meshcore.events import Event

from mesh2irc.common import ContactName, ChannelName, Message, MessageId, Contact

Destination = Union[ContactName, ChannelName]
DirectCallback = Callable[[Contact, Message, MessageId], Awaitable[None]]
ChannelCallback = Callable[[ChannelName, Message, MessageId], Awaitable[None]]


class Chatter(Protocol):

    async def update_contact(self, contact: Contact) -> None: ...

    # async def send_message(
    #     self, source: ContactName, message: Message, event: Event, *, channel_name: Optional[ChannelName] = None
    # ) -> None: ...

    async def send_direct(self, source: Contact, message: Message, event: Event) -> None: ...

    async def send_channel(
        self, source: ContactName, message: Message, event: Event, channel_name: ChannelName
    ) -> None: ...

    async def add_direct_callback(self, cb: DirectCallback) -> None: ...

    async def add_channel_callback(self, cb: ChannelCallback) -> None: ...


__all__ = ["Destination", "ChannelCallback", "Chatter"]
