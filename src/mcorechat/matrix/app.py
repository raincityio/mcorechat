import dataclasses
import logging
import ssl
from collections.abc import Callable, Awaitable
from pathlib import Path

import yaml
from aiohttp import web
from aiohttp.web_request import Request

from mcorechat.chatter import (
    MessageHandler,
    UnknownContactException,
    UnknownChannelException,
    ContactAlreadyAddedException,
    ChannelAlreadyAddedException,
    Chatter,
)
from mcorechat.common import Message, ChannelName, Contact, HTMLMessage, MessageId, ChannelDisplayName
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
    DisplayName,
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
class UserProfile:
    id: UserId
    display_name: DisplayName


@dataclasses.dataclass(frozen=True)
class RoomProfile:
    alias: RoomAlias
    name: RoomName


@dataclasses.dataclass(frozen=True)
class RoomInfo:
    id: RoomId
    alias: RoomAlias
    handler: MessageHandler
    admin_id: UserId
    identity_user_id: UserId


@dataclasses.dataclass(frozen=True)
class ContactInfo:
    user_id: UserId
    room_id: RoomId


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


class RoomManager:
    def __init__(self) -> None:
        self.rooms_by_room_id: dict[RoomId, RoomInfo] = {}
        self.rooms_by_alias: dict[RoomAlias, RoomInfo] = {}

    def add(self, room_info: RoomInfo) -> None:
        if room_info.id in self.rooms_by_room_id:
            raise ChannelAlreadyAddedException()
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


class Vanity:
    def __init__(self, config: Config, identity: Contact, identity_user_id: UserId) -> None:
        self.config = config
        self.identity = identity
        self.identity_user_id = identity_user_id

    def create_user_id(
        self,
        handle: ChannelDisplayName | Contact,
    ) -> UserId:
        match handle:
            case ChannelDisplayName():
                suffix = f"channel.{sha256(str(handle))}"
            case Contact() as contact:
                suffix = f"contact.{sha256(str(self.identity.public_key), str(contact.public_key))}"
        name = UserName(f"{self.config.app_namespace}.{suffix}")
        return UserId(name, self.config.domain)

    def create_display_name(self, handle: ChannelDisplayName | Contact):
        match handle:
            case ChannelDisplayName():
                display_name = DisplayName(str(handle))
            case Contact() as contact:
                display_name = DisplayName(f"{str(contact.name)}{self.config.contact_suffix}")
        return display_name

    def create_user_profile(self, handle: ChannelDisplayName | Contact):
        user_id = self.create_user_id(handle)
        display_name = self.create_display_name(handle)
        return UserProfile(user_id, display_name)

    def create_room_alias(self, handle: Contact | ChannelName):
        match handle:
            case ChannelName() as channel_name:
                suffix = f"channel.{sha256(str(self.identity.public_key), str(channel_name))}"
            case Contact() as contact:
                suffix = f"direct1.{sha256(str(self.identity.public_key), str(self.identity_user_id), str(contact.public_key))}"
        name = AliasName(f"{self.config.app_namespace}.{suffix}")
        return RoomAlias(name, self.config.domain)

    def create_room_profile(self, handle: Contact | ChannelName):
        match handle:
            case ChannelName() as channel_name:
                name = RoomName(str(channel_name))
            case Contact() as contact:
                name = RoomName(str(self.create_display_name(contact)))
        alias = self.create_room_alias(handle)
        return RoomProfile(alias, name)

    # TODO this is an awkward method now
    def create_space_profile(self, identity: Contact):
        alias = RoomAlias(
            AliasName(f"{self.config.app_namespace}.space.{identity.public_key}"),
            self.config.domain,
        )
        name = RoomName(str(identity.name))
        return RoomProfile(alias, name)


#### hmm
class MatrixChatter:

    def __init__(
        self,
        identity: Contact,
        identity_user_id: UserId,
        space_id: RoomId,
        *,
        vanity: Vanity,
        config: Config,
        room_manager: RoomManager,
        client: MatrixClient,
        contact_manager: ContactManager,
        mcm: "MatrixChatterManager",
    ):
        self.identity = identity
        self.identity_user_id = identity_user_id
        self.space_id = space_id
        self.vanity = vanity
        self.config = config
        self.room_manager = room_manager
        self.client = client
        self.contact_manager = contact_manager
        self.mcm = mcm

    # Interface
    async def prune_contacts(self, contacts: list[Contact]) -> None:
        contact_set = {self.vanity.create_user_id(e) for e in contacts}
        for room_member in await self.mcm.get_room_members(self.space_id):
            if not self.mcm.is_app_user_id(room_member.user_id):
                continue
            if room_member.user_id == self.config.app_user_id:
                continue
            if room_member.membership in (RoomMembership.JOIN, RoomMembership.INVITE):
                if room_member.user_id not in contact_set:
                    await self.client.leave_room(self.space_id, as_user_id=room_member.user_id)

    async def add_contact(self, contact: Contact, handler: MessageHandler) -> None:
        contact_profile = self.vanity.create_user_profile(contact)
        room_member = await self.ensure_room_member(self.space_id, contact_profile)
        if room_member.display_name != contact_profile.display_name:
            await self.client.set_display_name(contact_profile.id, contact_profile.display_name)
        room_profile = self.vanity.create_room_profile(contact)
        room_id = await self.ensure_room(room_profile, is_direct=True, as_user_id=contact_profile.id)
        room_info = RoomInfo(room_id, room_profile.alias, handler, contact_profile.id, self.identity_user_id)
        self.room_manager.add(room_info)
        contact_info = ContactInfo(contact_profile.id, room_id)
        self.contact_manager.add(contact_info)

    async def add_channel(self, channel_name: ChannelName, handler: MessageHandler) -> None:
        room_profile = self.vanity.create_room_profile(channel_name)
        room_id = await self.ensure_room(room_profile, is_direct=False)
        room_info = RoomInfo(room_id, room_profile.alias, handler, self.config.app_user_id, self.identity_user_id)
        self.room_manager.add(room_info)
        await self.send_room_invite(room_info.id, self.identity_user_id)

    async def remove_channel(self, channel_name: ChannelName) -> None:
        assert False

    async def remove_contact(self, contact: Contact) -> None:
        assert False

    async def send_direct(self, peer: Contact, message: Message | HTMLMessage) -> None:
        peer_user_id = self.vanity.create_user_id(peer)
        peer_info = self.contact_manager.get(peer_user_id)
        destination_room_member = await self.mcm.get_room_member(
            peer_info.room_id, self.identity_user_id, as_user_id=peer_info.user_id
        )
        if destination_room_member is None:
            invite = True
        elif destination_room_member.membership not in (RoomMembership.JOIN, RoomMembership.INVITE):
            invite = True
        else:
            invite = False
        if invite:
            await self.mcm.invite_user(peer_info.room_id, self.identity_user_id, as_user_id=peer_info.user_id)
        await self.client.send_message(peer_info.room_id, message, as_user_id=peer_info.user_id)

    async def send_channel(
        self, peer: ChannelDisplayName | Contact, channel_name: ChannelName, message: Message | HTMLMessage
    ) -> None:
        room_alias = self.vanity.create_room_alias(channel_name)
        room_info = self.room_manager.get(room_alias)
        peer_profile = self.vanity.create_user_profile(peer)
        await self.ensure_room_member(room_info.id, peer_profile)
        await self.client.send_message(room_info.id, message, as_user_id=peer_profile.id)

    # Support
    async def ensure_room(
        self,
        profile: RoomProfile,
        *,
        is_direct: bool,
        as_user_id: UserId | None = None,
        invite: list[UserId] | None = None,
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
                invite=invite,
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

    async def send_room_invite(self, room_id: RoomId, user_id: UserId) -> None:
        room_member = await self.mcm.get_room_member(room_id, user_id)
        if room_member is None:
            await self.mcm.invite_user(room_id, user_id)


class MatrixChatterManager:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.contact_manager = ContactManager()
        self.room_manager = RoomManager()
        # this is a cache, but is updated with transactions, so mostly correct
        self.room_members: dict[tuple[RoomId, UserId], RoomMember] = {}  # unbounded
        self.client = MatrixClient(
            config.homeserver, get_secret("as_token", config.app_as_token, config.app_as_token_path)
        )
        self.app_hs_token = get_secret("hs_token", config.app_hs_token, config.app_hs_token_path)
        self._event_handlers: dict[str, EventHandler] = {
            "m.room.member": self.handle_room_member,
            "m.room.message": self.handle_room_message,
        }
        self.chatters: dict[Contact, MatrixChatter] = {}

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
                        sender_user_id = parse_user_id(event["sender"])
                        peer_info = self.contact_manager.get(member.user_id)
                        await self.client.join_room(room_id, as_user_id=member.user_id)
                        await self.client.tombstone_room(room_id, peer_info.room_id, as_user_id=member.user_id)
                        await self.invite_user(peer_info.room_id, sender_user_id, as_user_id=member.user_id)

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
        room_id = RoomId(event["room_id"])
        if room_id not in self.room_manager:
            logger.debug(f"Room {room_id} not found")
            return
        room_info = self.room_manager.get(room_id)
        source_user_id = parse_user_id(event["sender"])
        if source_user_id != room_info.identity_user_id:
            logging.debug(f"{source_user_id} != {room_info.identity_user_id}")
            return
        # if self.is_app_user_id(source_user_id) or (source_user_id == self.config.app_user_id):
        #     logger.debug(f"Local user {source_user_id}")
        #     return

        message = Message(event["content"]["body"])
        message_id = MessageId(event["event_id"])

        try:
            await room_info.handler(message, message_id)
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
    ) -> Chatter:

        # TODO eventually support more interesting mappings?
        identity_user_id = self.config.identity_user_id
        vanity = Vanity(self.config, identity, identity_user_id)

        # set up space
        space_profile = vanity.create_space_profile(identity)
        space_id = await self.ensure_space(space_profile)
        await self.invite_user(space_id, identity_user_id)

        chatter = MatrixChatter(
            identity,
            identity_user_id,
            space_id,
            vanity=vanity,
            config=self.config,
            room_manager=self.room_manager,
            client=self.client,
            contact_manager=self.contact_manager,
            mcm=self,
        )
        if identity in self.chatters:
            raise Exception(f"Chatter for {identity} already exists")
        self.chatters[identity] = chatter
        return chatter
