#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Protocol

from meshcore.events import Event

from mesh2irc.common import ContactName, ChannelName, Message, MessageId, Contact, PublicKey

DirectCallback = Callable[[ContactName, PublicKey, Message, MessageId], Awaitable[None]]
ChannelCallback = Callable[[ContactName, ChannelName, Message, MessageId], Awaitable[None]]


class Chatter(Protocol):

    async def init(self) -> None: ...

    async def run(self) -> None: ...

    async def update_contact(self, contact: Contact) -> None: ...

    async def update_channel(
        self,
        channel_name: ChannelName,
    ) -> None: ...

    async def send_direct(self, source: Contact, destination: ContactName, message: Message, event: Event) -> None: ...

    async def send_channel(
        self, source: ContactName, message: Message, event: Event, channel_name: ChannelName
    ) -> None: ...

    async def send_channel_invite(self, contact_name: ContactName, channel_name: ChannelName) -> None: ...

    async def add_direct_callback(self, cb: DirectCallback) -> None: ...

    async def add_channel_callback(self, cb: ChannelCallback) -> None: ...

    async def advertise(self, public_key: PublicKey, *, contact: Contact | None = None) -> None: ...
