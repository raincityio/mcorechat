#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Protocol

from meshcore.events import Event

from mcorechat.common import ContactName, ChannelName, Message, MessageId, Contact, PublicKey, DisplayName

# identity, source, destination, message, message_id
DirectCallback = Callable[[PublicKey, DisplayName, PublicKey, Message, MessageId], Awaitable[None]]
# identity, source, destination, message, message_id
ChannelCallback = Callable[[PublicKey, DisplayName, ChannelName, Message, MessageId], Awaitable[None]]
CommandCallback = Callable[[PublicKey, DisplayName, Message], Awaitable[list[str]]]


class Chatter(Protocol):

    async def init(self, contact: Contact) -> None: ...

    async def run(self) -> None: ...

    async def update_contact(self, contact: Contact) -> None: ...

    async def send_direct(self, source: Contact, destination: ContactName, message: Message, event: Event) -> None: ...

    async def send_channel(
        self, contact: Contact, source: DisplayName, message: Message, event: Event, channel_name: ChannelName
    ) -> None: ...

    async def add_direct_callback(self, identity: PublicKey, source: DisplayName, cb: DirectCallback) -> None: ...

    async def add_channel_callback(
        self,
        contact: Contact,
        channel_name: ChannelName,
        cb: ChannelCallback,
        *,
        invitees: list[ContactName] | None = None,
    ) -> None: ...

    async def add_command_callback(
        self,
        contact: Contact,
        cb: CommandCallback,
        *,
        invitees: list[ContactName] | None = None,
    ) -> None: ...

    async def advertise(
        self, identity: PublicKey, public_key: PublicKey, *, contact: Contact | None = None
    ) -> None: ...
