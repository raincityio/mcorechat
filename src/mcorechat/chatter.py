#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Protocol

from meshcore.events import Event

from mcorechat.common import ContactName, ChannelName, Message, MessageId, Contact, PublicKey, DisplayName

# source, destination, message, message_id
DirectCallback = Callable[[DisplayName, PublicKey, Message, MessageId], Awaitable[None]]
# source, destination, message, message_id
ChannelCallback = Callable[[DisplayName, ChannelName, Message, MessageId], Awaitable[None]]
# source, message
CommandCallback = Callable[[DisplayName, Message], Awaitable[list[str]]]


class Chatter(Protocol):

    async def init(self, contact: Contact) -> None: ...

    async def run(self) -> None: ...

    async def update_contact(self, contact: Contact) -> None: ...

    async def send_direct(self, source: Contact, destination: ContactName, message: Message, event: Event) -> None: ...

    async def send_channel(
        self, identity: Contact, source: DisplayName, message: Message, event: Event, channel_name: ChannelName
    ) -> None: ...

    async def add_direct_callback(self, source: DisplayName, cb: DirectCallback) -> None: ...

    async def add_channel_callback(
        self,
        identity: Contact,
        channel_name: ChannelName,
        cb: ChannelCallback,
    ) -> None: ...

    async def add_command_callback(
        self,
        identity: Contact,
        cb: CommandCallback,
    ) -> None: ...

    async def advertise(self, public_key: PublicKey, *, contact: Contact | None = None) -> None: ...
