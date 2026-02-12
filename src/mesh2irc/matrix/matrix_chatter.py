#!/usr/bin/env python3
import asyncio
import json
import logging
from typing import cast

import aiohttp
import nio.rooms
from meshcore.events import Event
from nio import (
    JoinError,
    JoinedMembersError,
    LoginError,
    MatrixRoom,
    RoomCreateError,
    RoomInviteError,
    RoomPreset,
    RoomSendError,
    RoomVisibility,
    RoomMemberEvent,
    RoomMessage,
    SyncError,
    JoinedMembersResponse,
)
from nio.client import AsyncClient

from mesh2irc.chatter import ChannelCallback, DirectCallback
from mesh2irc.common import JSONEncoder, Message, MessageId, ChannelName, ContactName, Contact
from mesh2irc.matrix.common import UserId, SecretText, parse_user_id
from mesh2irc.matrix.config import Config

logger = logging.getLogger(__name__)


class MatrixChatter:
    def __init__(self, config: Config):
        self.config = config
        self.clients = dict[UserId, AsyncClient]()
        self.clients_lock = asyncio.Lock()
        self.admin_user_id = UserId(config.admin_user, config.domain)
        self.admin_client = AsyncClient(str(config.homeserver), str(self.admin_user_id))
        self.direct_callbacks = set[DirectCallback]()
        self.channel_callbacks = set[ChannelCallback]()

    async def init(self):
        resp = await self.admin_client.login(self.config.admin_password.value)
        if isinstance(resp, LoginError):
            raise Exception(f"Login failed: {resp}")

    async def update_contact(self, contact: Contact):
        logger.debug(f"Updating contact: {contact}")
        user_id = UserId.create_from_contact(contact, self.config.domain)
        if self.config.trusted_prefix is None:
            trusted_display_name = contact.name
        else:
            trusted_display_name = ContactName(f"{contact.name} {self.config.trusted_prefix}")
        await self.update_user(
            user_id,
            trusted_display_name,
            self.config.user_password,
        )

    async def add_direct_callback(self, cb: DirectCallback) -> None:
        self.direct_callbacks.add(cb)

    async def add_channel_callback(self, cb: ChannelCallback) -> None:
        self.channel_callbacks.add(cb)

    async def update_channel(self, channel_name: ChannelName) -> None:
        pass

    async def run(self):

        async def room_message_cb(room: MatrixRoom, event: nio.rooms.Event):
            source = parse_user_id(self.config.app_prefix, event.sender)
            if source != self.admin_user_id:
                return
            message = Message(event.source["content"]["body"])
            message_id = MessageId(event.event_id)
            if room.is_group:
                # private message, get the room members
                resp = await self.admin_client.joined_members(room.room_id)
                if type(resp) is JoinedMembersError:
                    raise Exception(str(resp.status_code))
                for member in cast(JoinedMembersResponse, resp).members:
                    member_user_id = parse_user_id(self.config.app_prefix, member.user_id)
                    if member_user_id != self.admin_user_id:
                        for cb in self.direct_callbacks:
                            public_key = member_user_id.public_key
                            if public_key is None:
                                # TODO message a warning
                                logger.warning(f"public_key is None: {member_user_id}")
                            else:
                                await cb(public_key, message, message_id)
            else:
                assert room.name is not None
                for cb in self.channel_callbacks:
                    await cb(ChannelName(room.name), message, message_id)

        self.admin_client.add_event_callback(room_message_cb, RoomMessage)

        async def room_member_cb(room: MatrixRoom, event: nio.rooms.Event):
            room_member_ev = cast(RoomMemberEvent, event)
            is_direct: bool = room_member_ev.content.get("is_direct", False)
            if not is_direct:
                return
            user_id = parse_user_id(self.config.app_prefix, room_member_ev.state_key)
            client = await self.get_client(user_id, self.config.user_password)
            resp = await client.join(room.room_id)
            if isinstance(resp, JoinError):
                raise Exception(str(resp.status_code))

        self.admin_client.add_event_callback(room_member_cb, RoomMemberEvent)

        # enter into forever loop
        await self.admin_client.sync_forever()

    def log_room(self, room: MatrixRoom):
        return
        logger.info(f"Room: {room.name}")
        logger.info(f"Room.is_group: {room.is_group}")
        logger.info(f"Room.group_name: {room.group_name()}")

    async def send_direct(self, source: Contact, message: Message, event: Event):
        source_user_id = UserId.create_from_contact(source, self.config.domain)
        # TODO may need to create a user
        source_client = await self.get_client(source_user_id, self.config.user_password)
        await source_client.sync(full_state=True)
        # TODO may need to create a room
        # TODO when i leave a room and reenter, then i have bad client state
        found_rooms = list[MatrixRoom]()
        for room in source_client.rooms.values():
            if not room.is_group:
                continue
            if room.group_name() == str(self.admin_user_id.name):
                found_rooms.append(room)
        if len(found_rooms) == 0:
            raise Exception(f"Source not found: {source}")
        for room in found_rooms:
            logger.error(f"Room: {room.name}")
            resp = await source_client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": message, "meshcore_event": json.dumps(event, cls=JSONEncoder)},
            )
            if isinstance(resp, RoomSendError):
                raise Exception(str(resp.status_code))

    async def send_channel(
        self, source: ContactName, message: Message, event: Event, channel_name: ChannelName
    ) -> None:
        source_user_id = UserId.create_from_contact_name(source, self.config.domain)
        try:
            source_client = await self.get_client(source_user_id, self.config.user_password)
        except Exception as e:
            logger.error(e)
            await self.update_user(source_user_id, source, self.config.user_password)
            source_client = await self.get_client(source_user_id, self.config.user_password)

        room = next(filter(lambda x: x.name == str(channel_name), source_client.rooms.values()), None)
        if room is None:
            logger.debug(f"Creating new room: {source_user_id} {channel_name}")
            resp = await self.admin_client.room_create(
                visibility=RoomVisibility.public,
                alias=str(channel_name),
                name=str(channel_name),
                topic=str(channel_name),
                preset=RoomPreset.public_chat,
            )
            if isinstance(resp, RoomCreateError):
                if resp.status_code != "M_ROOM_IN_USE":
                    raise Exception(str(resp.status_code))
            resp = await self.admin_client.sync(full_state=True)
            if isinstance(resp, SyncError):
                raise Exception(str(resp.status_code))

            room = next(filter(lambda x: x.name == str(channel_name), self.admin_client.rooms.values()))
            room_id = room.room_id
            resp = await self.admin_client.room_invite(room_id, str(source_user_id))
            if isinstance(resp, RoomInviteError):
                # strangely invite returns this if you do it twice for the same user
                if resp.status_code != "M_FORBIDDEN":
                    raise Exception(str(resp.status_code))
            resp = await source_client.join(room_id)
            if isinstance(resp, JoinError):
                raise Exception(str(resp.status_code))
        else:
            room_id = room.room_id

        resp = await source_client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message, "meshcore_event": json.dumps(event, cls=JSONEncoder)},
        )
        if isinstance(resp, RoomSendError):
            raise Exception(str(resp.status_code))

    async def login_user(self, user_id: UserId, user_password: SecretText) -> AsyncClient:
        logger.debug(f"Login user: {user_id} @ {self.config.homeserver}")
        client = AsyncClient(str(self.config.homeserver), str(user_id))
        resp = await client.login(user_password.value)
        if isinstance(resp, LoginError):
            await client.close()
            raise Exception(f"Login failed: {resp} {user_id}")
        return client

    async def get_client(self, user_id: UserId, password: SecretText) -> AsyncClient:
        client = self.clients.get(user_id, None)
        if client is None:
            async with self.clients_lock:
                client = await self.login_user(user_id, password)
                # await client.sync()
                new_clients = self.clients.copy()
                new_clients[user_id] = client
                self.clients = new_clients
            await client.sync(full_state=True)
        return client

    # async def join_user(self, channel_name: ChannelName, user_id: UserId):
    #     url = f"{self.config.homeserver}/_synapse/admin/v1/join/Public"
    #     payload = {
    #         "user_id": str(user_id),
    #     }
    #     headers = {"Authorization": f"Bearer {self.admin_client.access_token}", "Content-Type": "application/json"}
    #     async with aiohttp.ClientSession() as session:
    #         async with session.post(url, headers=headers, json=payload) as resp:
    #             if resp.status == 200 or resp.status == 201:
    #                 logger.debug(f"User joined: {user_id}")
    #             else:
    #                 text = await resp.text()
    #                 raise Exception(text)

    async def update_user(self, user_id: UserId, display_name: ContactName, user_password: SecretText):
        logger.error(f"Updating user: {user_id} @ {self.config.homeserver} {display_name}")
        url = f"{self.config.homeserver}/_synapse/admin/v2/users/{user_id}"
        payload = {
            "password": user_password.value,
            "admin": False,
            "deactivated": False,
            "displayname": str(display_name),
        }
        headers = {"Authorization": f"Bearer {self.admin_client.access_token}", "Content-Type": "application/json"}

        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=payload) as resp:
                if resp.status == 200 or resp.status == 201:
                    logger.debug(f"User created: {user_id}")
                else:
                    text = await resp.text()
                    raise Exception(f"Failed ({resp.status}): {text} {user_id}")
