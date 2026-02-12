#!/usr/bin/env python3

import dataclasses
import enum
import hashlib
from typing import Any, NewType, Optional

from mesh2irc.common import ContactName, Contact, PublicKey, ChannelName

UserName = NewType("UserName", str)
DomainName = NewType("DomainName", str)
HomeserverURL = NewType("HomeserverURL", str)
RoomId = NewType("RoomId", str)
DisplayName = NewType("DisplayName", str)
type MatrixEvent = dict[str, Any]


def shallow_copy(obj: Any) -> Any:
    return {field.name: getattr(obj, field.name) for field in dataclasses.fields(obj)}


@dataclasses.dataclass(frozen=True)
class SecretText:
    raw: str

    def __str__(self):
        return "********"

    def __repr__(self) -> str:
        return repr(str(self))


def sha256(text: str) -> str:
    utf8_bytes = text.encode("utf-8")
    sha256_hash = hashlib.sha256(utf8_bytes)
    return sha256_hash.hexdigest()


@dataclasses.dataclass(frozen=True)
class RoomAlias:
    name: ChannelName
    domain: DomainName

    def __str__(self):
        return f"#{self.name}:{self.domain}"

    @staticmethod
    def from_name(name: ChannelName, domain: DomainName, *, prefix: Optional[str] = None):
        if prefix is None:
            return RoomAlias(name, domain)
        name = ChannelName(f"{prefix}{name}")
        return RoomAlias(name, domain)


def parse_room_alias(raw: str):
    assert raw.startswith("#")
    raw_name, raw_domain = raw[1:].split(":", 1)
    return RoomAlias(ChannelName(raw_name), DomainName(raw_domain))


@dataclasses.dataclass(frozen=True)
class UserId:
    name: UserName
    domain: DomainName
    public_key: Optional[PublicKey] = None

    def __str__(self):
        return f"@{self.name}:{self.domain}"

    @staticmethod
    def create_from_contact(contact: Contact, domain: DomainName, *, prefix: Optional[str] = None):
        if prefix is None:
            user_name = UserName(f"t_{str(contact.public_key)}")
        else:
            user_name = UserName(f"{prefix}t_{str(contact.public_key)}")
        return UserId(user_name, DomainName(domain), contact.public_key)

    @staticmethod
    def create_from_contact_name(contact_name: ContactName, domain: DomainName, *, prefix: Optional[str] = None):
        if prefix is None:
            user_name = UserName(f"u_{sha256(contact_name.raw)}")
        else:
            user_name = UserName(f"{prefix}u_{sha256(contact_name.raw)}")
        return UserId(user_name, DomainName(domain))


def parse_user_id(app_prefix: str, raw: str):
    mesh_user_id_start = f"@{app_prefix}"
    if raw.startswith(mesh_user_id_start):
        prefixed_raw_user, raw_domain = raw[1:].split(":", 1)
        raw_user = prefixed_raw_user[len(app_prefix) :]
        if raw_user.startswith("t_"):
            public_key = PublicKey(raw_user[2:])
            return UserId(UserName(prefixed_raw_user), DomainName(raw_domain), public_key)
        elif raw_user.startswith("u_"):
            return UserId(UserName(prefixed_raw_user), DomainName(raw_domain), None)
        else:
            raise Exception(f"Invalid mesh user ID: {raw_user}")
    else:
        raw_user, raw_domain = raw[1:].split(":", 1)
        return UserId(UserName(raw_user), DomainName(raw_domain))


class RoomMembership(enum.Enum):
    INVITE = "invite"
    JOIN = "join"
    LEAVE = "leave"


@dataclasses.dataclass(frozen=True)
class RoomMember:
    user_id: UserId
    is_direct: bool
    membership: RoomMembership
    display_name: Optional[DisplayName]


@dataclasses.dataclass(frozen=True)
class ChannelRoom:
    room_id: RoomId
    name: Optional[ChannelName] = None
    members: dict[UserId, RoomMember] = dataclasses.field(default_factory=dict[UserId, RoomMember])
    alias: Optional[RoomAlias] = None

    def to_data(self):
        return shallow_copy(self)

    def copy(self, **kwargs: Any):
        return ChannelRoom(**(self.to_data() | kwargs))
