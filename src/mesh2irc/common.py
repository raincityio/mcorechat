#!/usr/bin/env python3
import dataclasses
import json
from typing import Any

from meshcore.events import Event, EventType


@dataclasses.dataclass(frozen=True)
class Message:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class MessageId:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class ChannelName:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class ContactName:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class PublicKeyPrefix:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class PublicKey:
    value: str

    def __str__(self):
        return self.value

    def startswith(self, prefix: PublicKeyPrefix):
        return self.value.startswith(prefix.value)


@dataclasses.dataclass(frozen=True)
class Channel:
    name: ChannelName
    idx: int


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
