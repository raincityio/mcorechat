#!/usr/bin/env python3
from typing import Optional, Protocol, NewType


class Identity(Protocol):
    pass


Message = NewType("Message", str)
ChannelName = NewType("ChannelName", str)
UserName = NewType("UserName", str)


class Chatter(Protocol):

    async def send_message(
        self, source: UserName, message: Message, *, channel_name: Optional[ChannelName] = None
    ) -> None: ...
