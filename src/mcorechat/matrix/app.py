import dataclasses
import logging
import ssl
from asyncio import TaskGroup
from collections.abc import Callable, Awaitable
from pathlib import Path

import yaml
from aiohttp import web
from aiohttp.web_request import Request

from mcorechat.chatter import (
    ChannelCallback,
    UnknownContactException,
    UnknownChannelException,
    DirectCallback,
    ContactAlreadyAddedException,
    ChannelAlreadyAddedException,
)
from mcorechat.common import ContactName, Message, ChannelName, Contact, HTMLMessage, MessageId, DisplayName
from mcorechat.matrix.common import (
    MatrixAPIError,
    MatrixEvent,
    RoomAlias,
    RoomId,
    UserId,
    RoomMember,
    parse_room_alias,
    RoomMembership,
    UserName,
    RoomName,
    SecretText,
    parse_user_id,
    sha256,
    RoomVisibility,
    AliasName,
)
from mcorechat.matrix.config import Config
from mcorechat.matrix.matrix_client import MatrixClient, parse_member_event

type EventHandler = Callable[[MatrixEvent], Awaitable[None]]

logger = logging.getLogger(__name__)


def get_secret(key_type: str, secret: SecretText | None, secret_path: Path | None) -> SecretText:
    match (secret, secret_path):
        case (SecretText(), None):
            return secret
        case (None, Path()):
            value = yaml.load(secret_path.read_text(), Loader=yaml.SafeLoader)
            return SecretText(value[key_type])
        case _:
            raise Exception("Exactly one of secret_path or secret must be provided")


@dataclasses.dataclass(frozen=True)
class ContactInfo:
    contact: Contact
    room_id: RoomId
    alias: RoomAlias
    user_id: UserId


class ContactManager:
    def __init__(self) -> None:
        self.contacts_by_user_id: dict[UserId, ContactInfo] = {}

    def add(self, contact_info: ContactInfo) -> None:
        if contact_info.user_id in self.contacts_by_user_id:
            raise ContactAlreadyAddedException()
        self.contacts_by_user_id[contact_info.user_id] = contact_info

    def get(self, user_id: UserId) -> ContactInfo:
        try:
            return self.contacts_by_user_id[user_id]
        except KeyError as e:
            raise UnknownContactException() from e

    def __contains__(self, user_id: UserId) -> bool:
        return user_id in self.contacts_by_user_id


type RoomHandler = Callable[[RoomId, UserId, Message, MessageId], Awaitable[None]]


@dataclasses.dataclass(frozen=True)
class RoomInfo:
    id: RoomId
    alias: RoomAlias
    handler: RoomHandler
    admin_id: UserId


class RoomManager:
    def __init__(self) -> None:
        self.rooms_by_room_id: dict[RoomId, RoomInfo] = {}
        self.rooms_by_alias: dict[RoomAlias, RoomInfo] = {}

    def add(self, room_info: RoomInfo) -> None:
        if room_info.id in self.rooms_by_room_id:
            raise ChannelAlreadyAddedException(room_info.name)
        if room_info.alias in self.rooms_by_alias:
            raise ChannelAlreadyAddedException()
        self.rooms_by_room_id[room_info.id] = room_info
        self.rooms_by_alias[room_info.alias] = room_info

    def get(self, handle: RoomId | RoomAlias) -> RoomInfo:
        try:
            match handle:
                case RoomId() as room_id:
                    return self.rooms_by_room_id[room_id]
                case RoomAlias() as alias:
                    return self.rooms_by_alias[alias]
        except KeyError as e:
            raise UnknownChannelException() from e

    def __contains__(self, handle: RoomId | RoomAlias) -> bool:
        match handle:
            case RoomId() as room_id:
                return room_id in self.rooms_by_room_id
            case RoomAlias() as alias:
                return alias in self.rooms_by_alias


@dataclasses.dataclass(frozen=True)
class UserProfile:
    id: UserId
    display_name: DisplayName


@dataclasses.dataclass(frozen=True)
class RoomProfile:
    alias: RoomAlias
    name: RoomName


class Vanity:
    def __init__(self, config: Config) -> None:
        self.config = config

    def create_user_id(
        self,
        identity: Contact,
        handle: DisplayName | Contact,
    ) -> UserId:
        match handle:
            case DisplayName() as display_name:
                user_name = UserName(f"{self.config.app_namespace}.channel.{sha256(str(display_name))}")
            case Contact() as contact:
                user_name = UserName(f"{self.config.app_namespace}.contact.{identity.public_key}.{contact.public_key}")
        return UserId(user_name, self.config.domain)

    def create_display_name(self, contact: Contact):
        return DisplayName(f"{str(contact.name)}{self.config.contact_suffix}")

    def create_user_profile(self, identity: Contact, handle: DisplayName | Contact):
        user_id = self.create_user_id(identity, handle)
        match handle:
            case DisplayName() as display_name:
                pass
            case Contact() as contact:
                display_name = self.create_display_name(contact)
        return UserProfile(user_id, display_name)

    def create_room_alias(self, identity: Contact, handle: Contact | ChannelName):
        match handle:
            case ChannelName() as channel_name:
                return RoomAlias(
                    AliasName(f"{self.config.app_namespace}.channel.{identity.public_key}.{sha256(str(channel_name))}"),
                    self.config.domain,
                )
            case Contact() as contact:
                return RoomAlias(
                    AliasName(f"{self.config.app_namespace}.direct.{identity.public_key}.{contact.public_key}"),
                    self.config.domain,
                )

    def create_room_profile(self, identity: Contact, handle: Contact | ChannelName):
        match handle:
            case ChannelName() as channel_name:
                name = RoomName(str(channel_name))
            case Contact() as contact:
                name = RoomName(str(self.create_display_name(contact)))
        alias = self.create_room_alias(identity, handle)
        return RoomProfile(alias, name)

    def create_space_profile(self, identity: Contact):
        alias = RoomAlias(
            AliasName(f"{self.config.app_namespace}.space.{identity.public_key}"),
            self.config.domain,
        )
        name = RoomName(str(identity.name))
        return RoomProfile(alias, name)

    def map_contact_name(self, contact_name: ContactName):
        if contact_name in self.config.contact_name_mappings:
            return self.config.contact_name_mappings[contact_name]
        else:
            return UserId(UserName(str(contact_name)), self.config.domain)


#### hmm
class MatrixChatter:

    def __init__(
        self,
        identity: Contact,
        space_id: RoomId,
        *,
        vanity: Vanity,
        config: Config,
        room_manager: RoomManager,
        channel_callback: ChannelCallback,
        direct_callback: DirectCallback,
        client: MatrixClient,
        contact_manager: ContactManager,
        app_user: UserId,
        mcm: "MatrixChatterManager",
    ):
        self.identity = identity
        self.space_id = space_id
        self.vanity = vanity
        self.config = config
        self.room_manager = room_manager
        self.channel_callback = channel_callback
        self.direct_callback = direct_callback
        self.client = client
        self.contact_manager = contact_manager
        self.app_user = app_user
        self.mcm = mcm

    # Interface
    async def add_contact(self, contact: Contact) -> None:
        async def handler(_room_id: RoomId, _source_user_id: UserId, _message: Message, _message_id: MessageId) -> None:
            _room_member = await self.mcm.get_room_member(_room_id, _source_user_id, as_user_id=contact_profile.id)
            if _room_member is None:
                return
            if _room_member.display_name is None:
                _source = DisplayName(str(_source_user_id.name))
            else:
                _source = _room_member.display_name
            if _source == identity_display_name:
                await self.direct_callback(_source, contact.public_key, _message, _message_id)

        identity_display_name = DisplayName(str(self.identity.name))
        contact_profile = self.vanity.create_user_profile(self.identity, contact)
        room_member = await self.ensure_room_member(self.space_id, contact_profile)
        if room_member.display_name != contact_profile.display_name:
            await self.client.set_display_name(contact_profile.id, contact_profile.display_name)
        room_profile = self.vanity.create_room_profile(self.identity, contact)
        room_id = await self.ensure_room(room_profile, is_direct=True, as_user_id=contact_profile.id)
        room_info = RoomInfo(room_id, room_profile.alias, handler, contact_profile.id)
        self.room_manager.add(room_info)
        contact_info = ContactInfo(contact, room_info.id, room_info.alias, contact_profile.id)
        self.contact_manager.add(contact_info)

    async def add_channel(self, channel_name: ChannelName, *, callback: ChannelCallback | None = None) -> None:
        callback = self.channel_callback if callback is None else callback

        async def handler(_room_id: RoomId, _source_user_id: UserId, _message: Message, _message_id: MessageId):
            _room_member = await self.mcm.get_room_member(_room_id, _source_user_id)
            if _room_member is None:
                return
            if _room_member.display_name is None:
                _source = DisplayName(str(_source_user_id.name))
            else:
                _source = _room_member.display_name
            if _source == identity_display_name:
                await callback(_source, channel_name, _message, _message_id)

        identity_display_name = DisplayName(str(self.identity.name))
        room_profile = self.vanity.create_room_profile(self.identity, channel_name)
        room_id = await self.ensure_room(room_profile, is_direct=False)
        room_info = RoomInfo(room_id, room_profile.alias, handler, self.app_user)
        self.room_manager.add(room_info)
        await self.send_room_invite(room_info.id, self.identity.name)

    async def send_direct(self, source: Contact, message: Message) -> None:
        source_user_id = self.vanity.create_user_id(self.identity, source)
        source_info = self.contact_manager.get(source_user_id)
        destination_user_id = self.vanity.map_contact_name(self.identity.name)
        destination_room_member = await self.mcm.get_room_member(
            source_info.room_id, destination_user_id, as_user_id=source_info.user_id
        )
        if destination_room_member is None:
            invite = True
        elif destination_room_member.membership not in (RoomMembership.JOIN, RoomMembership.INVITE):
            invite = True
        else:
            invite = False
        if invite:
            await self.mcm.invite_user(source_info.room_id, destination_user_id, as_user_id=source_info.user_id)
        await self.client.send_message(source_info.room_id, message, as_user_id=source_info.user_id)

    async def send_channel(self, source: DisplayName | Contact, channel_name: ChannelName, message: Message) -> None:
        room_alias = self.vanity.create_room_alias(self.identity, channel_name)
        room_info = self.room_manager.get(room_alias)
        source_profile = self.vanity.create_user_profile(self.identity, source)
        await self.ensure_room_member(room_info.id, source_profile)
        await self.client.send_message(room_info.id, message, as_user_id=source_profile.id)

    # Support
    async def ensure_room(
        self,
        profile: RoomProfile,
        *,
        is_direct: bool,
        as_user_id: UserId | None = None,
    ) -> RoomId:
        room_id = await self.client.get_room_id_by_alias(profile.alias)
        if room_id is None:
            if is_direct:
                visibility = RoomVisibility.PRIVATE
                preset = "trusted_private_chat"
            else:
                visibility = RoomVisibility.PUBLIC
                preset = "public_chat"
            room_id = await self.client.create_room(
                profile.name,
                profile.alias,
                visibility=visibility,
                preset=preset,
                is_direct=is_direct,
                as_user_id=as_user_id,
            )
            await self.client.state_attach_child(self.space_id, room_id, self.config.domain)
        else:
            test_room_name = await self.client.get_room_name(room_id, as_user_id=as_user_id)
            if profile.name != test_room_name:
                await self.client.set_room_name(room_id, profile.name, as_user_id=as_user_id)
        return room_id

    async def ensure_room_member(self, room_id: RoomId, profile: UserProfile):
        room_member = await self.mcm.get_room_member(room_id, profile.id)

        async def try_join():
            try:
                await self.client.join_room(room_id, as_user_id=profile.id)
            except MatrixAPIError as e:
                if e.errcode != "M_FORBIDDEN":
                    raise
                try:
                    await self.client.register_user(profile.id)
                    await self.client.set_display_name(profile.id, profile.display_name)
                except MatrixAPIError as e:
                    if e.errcode != "M_USER_IN_USE":
                        raise
                await self.client.join_room(room_id, as_user_id=profile.id)

        if room_member is None:
            await self.mcm.invite_user(room_id, profile.id)
            await try_join()
            room_member = await self.mcm.get_room_member(room_id, profile.id)
            assert room_member is not None
        else:
            if room_member.membership == RoomMembership.JOIN:
                pass
            elif room_member.membership == RoomMembership.INVITE:
                await try_join()
            else:
                raise Exception(f"Unexpected membership: {room_member.membership}")
        return room_member

    async def send_room_invite(self, room_id: RoomId, contact_name: ContactName) -> None:
        user_id = self.vanity.map_contact_name(contact_name)
        room_member = await self.mcm.get_room_member(room_id, user_id)
        if room_member is None:
            await self.mcm.invite_user(room_id, user_id)


class MatrixChatterManager:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.vanity = Vanity(config)
        self.contact_manager = ContactManager()
        self.room_manager = RoomManager()
        # this is a cache, but is updated with transactions, so mostly correct
        self.room_members: dict[tuple[RoomId, UserId], RoomMember] = {}  # unbounded
        self.app_user = UserId(config.app_user, config.domain)
        self.client = MatrixClient(
            config.homeserver, get_secret("as_token", config.app_as_token, config.app_as_token_path)
        )
        self.app_hs_token = get_secret("hs_token", config.app_hs_token, config.app_hs_token_path)
        self._event_handlers: dict[str, EventHandler] = {
            "m.room.member": self.handle_room_member,
            "m.room.message": self.handle_room_message,
        }

    async def run(self):
        app = web.Application()
        app.router.add_put(
            "/_matrix/app/v1/transactions/{txn_id}",
            self.transactions,
        )
        app.router.add_get(
            "/_matrix/app/v1/users/{user_id}",
            self.users,
        )
        app.router.add_get(
            "/_matrix/app/v1/rooms/{alias}",
            self.rooms,
        )

        runner = web.AppRunner(app)
        await runner.setup()

        if (self.config.ssl is not None) and self.config.ssl.enabled:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(self.config.ssl.certfile, self.config.ssl.keyfile)
        else:
            ssl_context = None

        host, port = self.config.listen
        site = web.TCPSite(runner, host=host, port=port, ssl_context=ssl_context)
        await site.start()
        logger.info(f"Started server on {host}:{port}")

    # Support
    async def ensure_space(
        self,
        profile: RoomProfile,
    ) -> RoomId:
        room_id = await self.client.get_room_id_by_alias(profile.alias)
        if room_id is None:
            room_id = await self.client.create_room(
                profile.name, profile.alias, visibility=RoomVisibility.PUBLIC, preset="public_chat", is_space=True
            )
        else:
            test_room_name = await self.client.get_room_name(room_id)
            if profile.name != test_room_name:
                await self.client.set_room_name(room_id, profile.name)
        return room_id

    async def get_room_member(
        self, room_id: RoomId, user_id: UserId, *, as_user_id: UserId | None = None
    ) -> RoomMember | None:
        key = (room_id, user_id)
        if key in self.room_members:
            return self.room_members[key]
        member = await self.client.get_room_member(room_id, user_id, as_user_id=as_user_id)
        if member is not None:
            self.room_members[key] = member
        return member

    async def get_room_members(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        members = await self.client.get_room_members(room_id, as_user_id=as_user_id)
        for member in members:
            key = (room_id, member.user_id)
            self.room_members[key] = member
        return members

    # invite will throw 403 (forbidden) if the user was already invited or is in the room
    async def invite_user(
        self,
        room_id: RoomId,
        user_id: UserId,
        *,
        ignore_forbidden: bool | None = None,
        as_user_id: UserId | None = None,
    ):
        ignore_forbidden = True if ignore_forbidden is None else ignore_forbidden
        try:
            await self.client.invite_user(room_id, user_id, as_user_id=as_user_id)
        except MatrixAPIError as e:
            if not ((e.status == 403) and ignore_forbidden):
                raise

    # service app interface
    async def handle_room_member(self, event: MatrixEvent) -> None:
        room_id = RoomId(event["room_id"])
        member = parse_member_event(event)
        key = (room_id, member.user_id)
        self.room_members[key] = member
        if member.membership == RoomMembership.INVITE:
            if room_id in self.room_manager:
                room_alias = self.room_manager.get(room_id).alias
            else:
                room_alias = None
            if self.is_app_user_id(member.user_id):
                if self.is_channel_room_alias(room_alias):
                    await self.client.join_room(room_id, as_user_id=member.user_id)
                else:
                    # ok, not a channel, join if it's direct and the contact is direct, and add app user too for management
                    if member.is_direct and (member.user_id in self.contact_manager):  # self.is_direct_member(member):
                        await self.client.join_room(room_id, as_user_id=member.user_id)
                        contact_info = self.contact_manager.get(member.user_id)
                        sender_user_id = parse_user_id(event["sender"])
                        await self.invite_user(contact_info.room_id, sender_user_id, as_user_id=member.user_id)
                        await self.client.tombstone_room(room_id, contact_info.room_id, as_user_id=member.user_id)

    def is_channel_room_alias(self, alias: RoomAlias | None) -> bool:
        if alias is None:
            return False
        return str(alias.name).startswith(f"{self.config.app_namespace}.channel.")

    def is_app_user_id(self, user_id: UserId):
        return user_id.name.startswith(self.config.app_namespace)

    async def send_error(
        self, room_id: RoomId, msg: str, *, cause: str | None = None, as_user_id: UserId | None = None
    ) -> None:
        if cause is None:
            await self.client.send_message(room_id, HTMLMessage(f"<i><b>{msg}</b></i>"), as_user_id=as_user_id)
        else:
            await self.client.send_message(room_id, HTMLMessage(f"<i><b>{msg}:</b> {cause}</i>"), as_user_id=as_user_id)

    async def handle_room_message(self, event: MatrixEvent) -> None:
        source_user_id = parse_user_id(event["sender"])
        if self.is_app_user_id(source_user_id) or (source_user_id == self.app_user):
            logger.debug(f"Local user {source_user_id}")
            return
        room_id = RoomId(event["room_id"])
        if room_id not in self.room_manager:
            logger.debug(f"Room {room_id} not found")
            return
        room_info = self.room_manager.get(room_id)

        message = Message(event["content"]["body"])
        message_id = MessageId(event["event_id"])

        try:
            await room_info.handler(room_id, source_user_id, message, message_id)
        except Exception as e:
            await self.send_error(room_id, "Failed to send message", cause=str(e), as_user_id=room_info.admin_id)

    async def transactions(self, request: Request):
        try:
            await self.transactions_(request)
            return web.json_response({})
        except Exception as e:
            logger.exception(e)
            if self.config.dev_soft_fail:
                return web.json_response({})
            return web.json_response({}, status=500)

    async def transactions_(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        payload = await request.json()
        events = payload.get("events", [])
        for event in events:
            logger.debug(event)
            handler = self._event_handlers.get(event["type"])
            if handler is not None:
                await handler(event)

    async def users(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        raw_user_id = request.match_info["user_id"]
        user_id = parse_user_id(raw_user_id)
        if user_id in self.contact_manager:
            return web.json_response({})
        return web.json_response({}, status=404)

    async def rooms(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        raw_alias = request.match_info["alias"]
        alias = parse_room_alias(raw_alias)
        if alias in self.room_manager:
            return web.json_response({})
        return web.json_response({}, status=404)

    def verify_as_token(self, request: Request):
        token = request.headers.get("Authorization")
        if token != f"Bearer {self.app_hs_token.value}":
            raise web.HTTPUnauthorized()

    async def add_chatter(
        self,
        identity: Contact,
        *,
        channels: list[ChannelName] | None = None,
        channel_callback: ChannelCallback,
        contacts: list[Contact] | None = None,
        direct_callback: DirectCallback,
    ) -> MatrixChatter:
        channels = [] if channels is None else channels
        contacts = [] if contacts is None else contacts

        # set up space
        space_profile = self.vanity.create_space_profile(identity)
        space_id = await self.ensure_space(space_profile)
        await self.invite_user(space_id, self.app_user)

        chatter = MatrixChatter(
            identity,
            space_id,
            vanity=self.vanity,
            config=self.config,
            room_manager=self.room_manager,
            channel_callback=channel_callback,
            direct_callback=direct_callback,
            client=self.client,
            contact_manager=self.contact_manager,
            app_user=self.app_user,
            mcm=self,
        )

        # remove old contacts from the space and add all new ones
        contact_set = {self.vanity.create_user_id(identity, e) for e in contacts}
        for room_member in await self.get_room_members(space_id):
            if not self.is_app_user_id(room_member.user_id):
                continue
            if room_member.user_id == self.app_user:
                continue
            if room_member.membership in (RoomMembership.JOIN, RoomMembership.INVITE):
                if room_member.user_id not in contact_set:
                    await self.client.leave_room(space_id, as_user_id=room_member.user_id)

        async with TaskGroup() as g:

            async def add_contact(_contact: Contact):
                await chatter.add_contact(_contact)

            for contact in contacts:
                g.create_task(add_contact(contact))

        for channel in channels:
            await chatter.add_channel(channel)

        return chatter
