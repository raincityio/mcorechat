#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Protocol

from mcorechat.common import ChannelName, Message, MessageId, Contact, PublicKey, DisplayName

# source, destination, message, message_id
type DirectCallback = Callable[[DisplayName, PublicKey, Message, MessageId], Awaitable[None]]
# source, destination, message, message_id
type ChannelCallback = Callable[[DisplayName, ChannelName, Message, MessageId], Awaitable[None]]


class ChannelAlreadyAddedException(Exception):
    pass


class UnknownChannelException(Exception):
    pass


class ContactAlreadyAddedException(Exception):
    pass


class UnknownContactException(Exception):
    pass


class Chatter(Protocol):
    async def add_contact(self, contact: Contact) -> None: ...
    async def add_channel(self, channel_name: ChannelName, *, callback: ChannelCallback | None = None) -> None: ...
    async def send_direct(self, source: Contact, message: Message) -> None: ...
    async def send_channel(
        self, source: Contact | DisplayName, channel_name: ChannelName, message: Message
    ) -> None: ...


class ChatterManager(Protocol):
    async def run(self) -> None: ...
    async def add_chatter(
        self,
        identity: Contact,
        *,
        channels: list[ChannelName] | None = None,
        channel_callback: ChannelCallback,
        contacts: list[Contact] | None = None,
        direct_callback: DirectCallback,
    ) -> Chatter: ...
