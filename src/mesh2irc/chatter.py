#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Protocol

from meshcore.events import Event

from mesh2irc.common import ContactName, ChannelName, Message, MessageId, Contact, PublicKey

DirectCallback = Callable[[ContactName, PublicKey, Message, MessageId], Awaitable[None]]
# identity, source, destination, message, message_id
ChannelCallback = Callable[[Contact, ContactName, ChannelName, Message, MessageId], Awaitable[None]]


class Chatter(Protocol):

    async def init(self, identity: Contact) -> None: ...

    async def run(self) -> None: ...

    async def update_contact(self, contact: Contact) -> None: ...

    # async def update_channel(
    #     self,
    #     identity: Contact,
    #     channel_name: ChannelName,
    # ) -> None: ...

    async def send_direct(self, source: Contact, destination: ContactName, message: Message, event: Event) -> None: ...

    async def send_channel(
        self, identity: Contact, source: ContactName, message: Message, event: Event, channel_name: ChannelName
    ) -> None: ...

    async def add_direct_callback(self, cb: DirectCallback) -> None: ...

    async def add_channel_callback(self, identity: Contact, channel_name: ChannelName, cb: ChannelCallback) -> None: ...

    async def advertise(self, identity: Contact, public_key: PublicKey, *, contact: Contact | None = None) -> None: ...
