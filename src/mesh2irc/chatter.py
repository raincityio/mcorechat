#!/usr/bin/env python3
from typing import Optional, Protocol, NewType

from meshcore.events import Event

Message = NewType("Message", str)
ChannelName = NewType("ChannelName", str)
UserName = NewType("UserName", str)


class Chatter(Protocol):

    async def send_message(
        self, source: UserName, message: Message, event: Event, *, channel_name: Optional[ChannelName] = None
    ) -> None: ...
