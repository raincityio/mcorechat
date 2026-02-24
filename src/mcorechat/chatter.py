#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Protocol

from mcorechat.common import ContactName, ChannelName, Message, MessageId, Contact, PublicKey, DisplayName

# source, destination, message, message_id
DirectCallback = Callable[[DisplayName, PublicKey, Message, MessageId], Awaitable[None]]
# source, destination, message, message_id
ChannelCallback = Callable[[DisplayName, ChannelName, Message, MessageId], Awaitable[None]]
# source, message
CommandCallback = Callable[[DisplayName, Message], Awaitable[list[str]]]


class UnknownChannelException(Exception):
    pass


class UnknownContactException(Exception):
    pass


class Chatter(Protocol):

    async def init(self, contact: Contact) -> None: ...

    async def run(self) -> None: ...

    async def add_contact(self, identity: Contact, contact: Contact, cb: DirectCallback) -> None: ...

    async def send_direct(
        self, identity: Contact, source: Contact, destination: ContactName, message: Message
    ) -> None: ...

    async def add_channel(
        self,
        identity: Contact,
        channel_name: ChannelName,
        cb: ChannelCallback,
    ) -> None: ...

    async def send_channel(
        self, identity: Contact, source: DisplayName, message: Message, channel_name: ChannelName
    ) -> None: ...

    async def add_command_callback(
        self,
        identity: Contact,
        cb: CommandCallback,
    ) -> None: ...

    async def advertise(self, identity: Contact, public_key: PublicKey, *, contact: Contact | None = None) -> None: ...
