#!/usr/bin/env python3
from collections.abc import Callable, Awaitable
from typing import Protocol

from mcorechat.common import ChannelName, Message, MessageId, Contact, DisplayName, HTMLMessage

# source, destination, message, message_id
type DirectCallback = Callable[[Message, MessageId], Awaitable[None]]
# source, destination, message, message_id
type ChannelCallback = Callable[[Message, MessageId], Awaitable[None]]


class InvalidRequestException(Exception):
    pass


class ChannelAlreadyAddedException(Exception):
    pass


class UnknownChannelException(Exception):
    pass


class ContactAlreadyAddedException(Exception):
    pass


class UnknownContactException(Exception):
    pass


class Chatter(Protocol):
    async def prune_contacts(self, contacts: list[Contact]) -> None: ...
    async def add_contact(self, contact: Contact, callback: DirectCallback) -> None: ...
    async def remove_contact(self, contact: Contact) -> None: ...
    async def add_channel(self, channel_name: ChannelName, callback: ChannelCallback) -> None: ...
    async def remove_channel(self, channel_name: ChannelName) -> None: ...
    async def send_direct(self, source: Contact, message: Message | HTMLMessage) -> None: ...
    async def send_channel(
        self, source: Contact | DisplayName, channel_name: ChannelName, message: Message | HTMLMessage
    ) -> None: ...


class ChatterManager(Protocol):
    async def run(self) -> None: ...
    async def add_chatter(
        self,
        identity: Contact,
    ) -> Chatter: ...
