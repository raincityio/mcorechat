import asyncio
import contextlib
import logging
from typing import Any, Optional

from aiohttp import web
from aiohttp.web_exceptions import HTTPBadRequest, HTTPUnauthorized
from aiohttp.web_request import Request
from meshcore.events import Event

from mesh2irc.chatter import DirectCallback, ChannelCallback
from mesh2irc.common import ContactName, Message, ChannelName, Contact
from mesh2irc.matrix import common
from mesh2irc.matrix.common import (
    MatrixEvent,
    RoomAlias,
    RoomId,
    UserId,
    RoomMember,
    ChannelRoom,
    UserName,
    parse_room_alias,
    RoomMembership,
)
from mesh2irc.matrix.config import Config
from mesh2irc.matrix.matrix_client import MatrixClient

logger = logging.getLogger(__name__)


class MatrixASChatter:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.direct_callbacks = list[DirectCallback]()
        self.channel_callbacks = list[ChannelCallback]()
        self.room_cache: dict[RoomId, ChannelRoom] = {}
        self.room_cache_lock = asyncio.Lock()
        self.user_cache: dict[UserId, ContactName] = {}
        self.admin_user = UserId(config.admin_user, config.domain)
        self.app_user = UserId(UserName(config.app_user), config.domain)
        self.client = MatrixClient(config.homeserver, config.app_as_token)
        self.discovery_room_id: Optional[RoomId] = None

    ## Resource Helpers
    def create_user_id(
        self, *, contact_name: Optional[ContactName] = None, contact: Optional[Contact] = None
    ) -> UserId:
        if contact_name is not None:
            return UserId.create_from_contact_name(contact_name, self.config.domain, prefix=self.config.app_prefix)
        elif contact is not None:
            return UserId.create_from_contact(contact, self.config.domain, prefix=self.config.app_prefix)
        else:
            raise Exception(f"No contact name or contact provided")

    def parse_user_id(self, raw: str):
        return common.parse_user_id(self.config.app_prefix, raw)

    def create_room_alias(self, room_name: ChannelName):
        return RoomAlias.from_name(room_name, self.config.domain, prefix=self.config.app_prefix)

    ## Lifecycle
    async def init(self):
        # await self.cleanup()
        # FIXME this doesn't work
        # await self.client.set_display_name(self.app_user, ContactName("Admin"))
        if self.config.enabled_discovery_room:
            discovery_room_name = self.config.discovery_room_name
            discovery_room_alias = self.create_room_alias(discovery_room_name)
            self.discovery_room_id = (await self.ensure_room(discovery_room_name, discovery_room_alias)).room_id

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

        site = web.TCPSite(runner, host="0.0.0.0", port=9000)
        await site.start()
        logger.debug(f"Started server on {9000}")

        # channel_name = ChannelName("Public")
        # room_alias = self.create_room_alias(room_name=channal_name)
        # source = ContactName("tesla")
        # message = Message("meow")
        # print("Send A")
        # await self.send_channel(source, message, None, channel_name)
        # print("Send B")
        # await self.send_channel(source, message, None, channel_name)
        # print("Send C")
        # room_alias = self.create_room_alias(RoomName("barf"))
        # await self.delete_room_alias(room_alias)
        # room_id = await self.create_room(room_alias.name, invite=[self.admin_user])
        # room_id = await self.get_room_id_by_alias(room_alias)
        # await self.set_visi(room_id)

    async def cleanup(self):
        for room_id in await self.client.get_public_rooms():
            for room_alias in await self.client.get_room_aliases(room_id):
                await self.client.delete_room_alias(room_alias)
            await self.client.set_room_visibility(room_id, "private")
        raise Exception("DONE")

    ## Interface
    async def update_contact(self, contact: Contact) -> None:
        user_id = self.create_user_id(contact=contact)
        await self.ensure_user(user_id, contact.name)
        if self.discovery_room_id is not None:
            await self.ensure_room_join_membership(self.room_cache[self.discovery_room_id], user_id)

    async def update_channel(self, channel_name: ChannelName) -> None:
        room_alias = self.create_room_alias(room_name=channel_name)
        await self.ensure_room(channel_name, room_alias)

    async def send_direct(self, source: Contact, message: Message, event: Event) -> None:
        logger.error(f"Direct not supported yet!")

    async def ensure_room(self, room_name: ChannelName, room_alias: RoomAlias):
        room = next(filter(lambda x: room_alias == x.alias, self.room_cache.values()), None)
        if room is not None:
            return room
        room_id = await self.client.get_room_id_by_alias(room_alias)
        if room_id is not None:
            return await self.get_room(room_id)
        room_id = await self.client.create_room(room_name, room_alias, invite=[self.admin_user])
        return await self.get_room(room_id)

    async def ensure_user(self, user_id: UserId, display_name: ContactName) -> None:
        if user_id.public_key is not None:
            display_name = ContactName(f"{self.config.trusted_suffix}{display_name}")
        test_display_name = self.user_cache.get(user_id, None)
        if test_display_name == display_name:
            return
        try:
            await self.client.register_user(user_id)
        except HTTPBadRequest:
            pass
        await self.client.set_display_name(user_id, display_name)
        self.user_cache[user_id] = display_name

    async def ensure_room_join_membership(self, room: ChannelRoom, user_id: UserId):
        room_member = room.members.get(user_id)
        if room_member is None:
            try:
                await self.client.invite_user(room.room_id, user_id)
            except HTTPUnauthorized:
                logger.warning(f"duplicate invite")
                pass
            await self.client.join_room(room.room_id, as_user_id=user_id)
        else:
            if room_member.membership == RoomMembership.INVITE:
                await self.client.join_room(room.room_id, as_user_id=user_id)

    async def send_channel(
        self, source: ContactName, message: Message, event: Event, channel_name: ChannelName
    ) -> None:
        room_alias = self.create_room_alias(room_name=channel_name)
        room = await self.ensure_room(channel_name, room_alias)
        source_user_id = self.create_user_id(contact_name=source)
        await self.ensure_user(source_user_id, source)
        await self.ensure_room_join_membership(room, source_user_id)
        await self.client.send_message(room.room_id, message, as_user_id=source_user_id)

    async def add_direct_callback(self, cb: DirectCallback) -> None:
        self.direct_callbacks.append(cb)

    async def add_channel_callback(self, cb: ChannelCallback) -> None:
        self.channel_callbacks.append(cb)

    @contextlib.asynccontextmanager
    async def update_room(self, room_id: RoomId):

        class Updater:
            def __init__(self, _room: Optional[ChannelRoom]):
                self.room = _room

            def __call__(self, **_kwargs: Any):
                assert self.room is not None
                self.room = self.room.copy(**_kwargs)

            def set(self, _room: ChannelRoom):
                assert self.room is None
                self.room = _room

        async with self.room_cache_lock:
            room = self.room_cache.get(room_id, None)
            updater = Updater(room)
            yield updater
            if updater.room is not None:
                self.room_cache[room_id] = updater.room

    async def handle_room_create(self, event: MatrixEvent) -> None:
        # m.room.create
        room_id = RoomId(event["room_id"])
        async with self.update_room(room_id) as updater:
            if updater.room is None:
                updater.set(ChannelRoom(room_id))

    def parse_room_name(self, event: MatrixEvent):
        raw_name = event.get("content", {}).get("name", None)
        raw_name = None if raw_name == "" else raw_name
        return None if raw_name is None else ChannelName(raw_name)

    async def handle_room_name(self, event: MatrixEvent) -> None:
        # m.room.name
        room_id = RoomId(event["room_id"])
        async with self.update_room(room_id) as updater:
            if updater.room is not None:
                name = self.parse_room_name(event)
                updater(name=name)

    def parse_room_canonical_alias(self, event: MatrixEvent):
        raw_alias = event.get("content", {}).get("alias", None)
        raw_alias = None if raw_alias == "" else raw_alias
        return None if raw_alias is None else parse_room_alias(raw_alias)

    async def handle_room_canonical_alias(self, event: MatrixEvent) -> None:
        # m.room.canonical_alias
        room_id = RoomId(event["room_id"])
        async with self.update_room(room_id) as updater:
            if updater.room is not None:
                alias = self.parse_room_canonical_alias(event)
                updater(alias=alias)

    def parse_member(self, event: MatrixEvent):
        user_id = self.parse_user_id(event["state_key"])
        is_direct = event["content"].get("is_direct", False)
        membership = RoomMembership(event["content"]["membership"])
        return RoomMember(user_id, is_direct, membership)

    async def handle_room_member(self, event: MatrixEvent) -> None:
        # m.room.member
        room_id = RoomId(event["room_id"])
        member = self.parse_member(event)
        async with self.update_room(room_id) as updater:
            if updater.room is not None:
                new_members = updater.room.members.copy()
                new_members[member.user_id] = member
                updater(members=new_members)
        if member.membership == RoomMembership.INVITE:
            if member.user_id != self.admin_user:
                await self.client.join_room(room_id, as_user_id=member.user_id)

    def is_direct(self, room: ChannelRoom):
        if room.name is not None:
            return False
        for member in room.members.values():
            if member.user_id == self.admin_user:
                continue
            elif member.user_id == self.app_user:
                continue

    async def handle_room_message(self, event: MatrixEvent) -> None:
        # m.room.message
        event_id = event["event_id"]
        user_id = self.parse_user_id(event["sender"])
        if user_id != self.admin_user:
            return
        room_id = RoomId(event["room_id"])
        room = await self.get_room(room_id)
        message = Message(event["content"]["body"])
        if room.name is None:
            logger.error("DIRECT NOT SUPPORTED YET")
        else:
            for cb in self.channel_callbacks:
                await cb(room.name, message, event_id)

    async def get_room(self, room_id: RoomId, *, as_user_id: Optional[UserId] = None):
        if room_id in self.room_cache:
            return self.room_cache[room_id]
        async with self.update_room(room_id) as updater:
            if updater.room is None:
                state = await self.client.get_room_state(room_id, as_user_id=as_user_id)
                room_name: Optional[ChannelName] = None
                members: dict[UserId, RoomMember] = {}
                alias: Optional[RoomAlias] = None
                for event in state:
                    if event["type"] == "m.room.member":
                        member = self.parse_member(event)
                        members[member.user_id] = member
                    elif event["type"] == "m.room.name":
                        room_name = self.parse_room_name(event)
                    elif event["type"] == "m.room.canonical_alias":
                        alias = self.parse_room_canonical_alias(event)
                room = ChannelRoom(room_id, room_name, members, alias)
                updater.set(room)
            else:
                room = updater.room
        return room

    async def transactions(self, request: Request):
        logger.debug(request)
        try:
            self.verify_as_token(request)
            payload = await request.json()
            events = payload.get("events", [])
            for event in events:
                logger.debug(event)
                if event["type"] == "m.room.create":
                    await self.handle_room_create(event)
                elif event["type"] == "m.room.member":
                    await self.handle_room_member(event)
                elif event["type"] == "m.room.canonical_alias":
                    await self.handle_room_canonical_alias(event)
                elif event["type"] == "m.room.name":
                    await self.handle_room_name(event)
                elif event["type"] in "m.room.message":
                    await self.handle_room_message(event)
        except Exception as e:
            logging.exception(e)
            # raise
        return web.json_response({})

    async def users(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        return web.json_response({})

    # TODO consider returning 404 for all here
    async def rooms(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        raw_alias = request.match_info["alias"]
        alias = parse_room_alias(raw_alias)
        if alias.domain != self.config.domain:
            return web.json_response({}, status=404)
        found = next(filter(lambda x: x.name == alias.name, self.room_cache.values()), None)
        if found is None:
            return web.json_response({}, status=404)
        return web.json_response({}, status=200)

    def verify_as_token(self, request: Request):
        token = request.headers.get("Authorization")
        if token != f"Bearer {self.config.app_hs_token.raw}":
            raise web.HTTPUnauthorized()
