#!/usr/bin/env python3
import dataclasses
import json
from typing import Any, NewType

from meshcore.events import Event, EventType

Message = NewType("Message", str)
MessageId = NewType("MessageId", str)
# ChannelName = NewType("ChannelName", str)
# UserName = NewType("UserName", str)


# @dataclasses.dataclass(frozen=True)
# class Message:
#     content: str
#     id: str


@dataclasses.dataclass(frozen=True)
class ChannelName:
    raw: str

    def __str__(self) -> str:
        return self.raw


@dataclasses.dataclass(frozen=True)
class ContactName:
    raw: str

    def __str__(self) -> str:
        return self.raw


@dataclasses.dataclass(frozen=True)
class Channel:
    name: ChannelName
    idx: int


# ContactName = NewType("ContactName", str)
PublicKey = NewType("PublicKey", str)
PublicKeyPrefix = NewType("PublicKeyPrefix", str)


@dataclasses.dataclass(frozen=True)
class Contact:
    name: ContactName
    public_key: PublicKey


class JSONEncoder(json.JSONEncoder):
    def default(self, o: Any):
        if isinstance(o, Event):
            data = {
                "type": o.type,
                "payload": o.payload,
                "attributes": o.attributes,
            }
            return data
        elif isinstance(o, EventType):
            return o.name
        elif isinstance(o, bytes):
            return o.hex()
        return super().default(o)


__all__ = [
    "Message",
    "MessageId",
    "ChannelName",
    "Channel",
    "ContactName",
    "PublicKey",
    "PublicKeyPrefix",
    "Contact",
    "JSONEncoder",
]
