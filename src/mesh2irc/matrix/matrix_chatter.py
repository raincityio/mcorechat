#!/usr/bin/env python3
import asyncio
import json
import logging
from typing import Optional

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
)
from nio.client import AsyncClient

from mesh2irc.chatter import ChannelName, Message, UserName, ChannelCallback, MessageId
from mesh2irc.common import JSONEncoder
from mesh2irc.matrix.common import UserId, SecretText
from mesh2irc.matrix.config import Config


class MatrixChatter:
    def __init__(self, config: Config):
        self.config = config
        self.clients = dict[UserId, AsyncClient]()
        self.clients_lock = asyncio.Lock()
        self.admin_user_id = UserId(config.admin_user, config.domain)
        self.admin_client = AsyncClient(config.homeserver, str(self.admin_user_id))
        self.channel_callbacks = set[ChannelCallback]()

    async def add_message_callback(self, cb: ChannelCallback) -> None:
        self.channel_callbacks.add(cb)

    async def remove_message_callback(self, cb: ChannelCallback) -> None:
        self.channel_callbacks.remove(cb)

    async def run(self):
        resp = await self.admin_client.login(self.config.admin_password.raw)
        if isinstance(resp, LoginError):
            raise Exception(f"Login failed: {resp}")
        # await self.join_user(SecretText(resp.access_token), ChannelName("Public"), self.admin_user_id)

        async def room_message_cb(room: MatrixRoom, event: nio.rooms.Event):
            message = Message(event.source["content"]["body"])
            message_id = MessageId(event.event_id)
            source = UserId.parse_user_id(event.sender)
            if source != self.admin_user_id:
                return
            if room.is_group:
                # private message, get the room members
                resp = await self.admin_client.joined_members(room.room_id)
                if type(resp) is JoinedMembersError:
                    raise Exception(str(resp.status_code))
                for member in resp.members:
                    member_user_id = UserId.parse_user_id(member.user_id)
                    user_name = UserName(member.display_name)
                    if member_user_id == self.admin_user_id:
                        logging.debug("FOUND ADMIN")
                    else:
                        for cb in self.channel_callbacks:
                            await cb(source.name, user_name, message, message_id)
                pass
            else:
                for cb in self.channel_callbacks:
                    await cb(source.name, ChannelName(room.name), message, message_id)

        self.admin_client.add_event_callback(room_message_cb, RoomMessage)

        async def room_member_cb(room: MatrixRoom, event: nio.rooms.Event):
            is_direct = event.content.get("is_direct", False)
            if not is_direct:
                return
            user_name = UserName(event.content["displayname"])
            user_id = UserId.create_hashed_user_id(user_name, self.config.domain)
            client = await self.get_client(user_id, self.config.user_password)
            resp = await client.join(room.room_id)
            if isinstance(resp, JoinError):
                raise Exception(str(resp.status_code))

        self.admin_client.add_event_callback(room_member_cb, RoomMemberEvent)
        await self.admin_client.sync_forever()

    def log_room(self, room: MatrixRoom):
        return
        logging.info(f"Room: {room.name}")
        logging.info(f"Room.is_group: {room.is_group}")
        logging.info(f"Room.group_name: {room.group_name()}")

    async def send_direct_message(self, source: UserName, message: Message, event: Event):
        logging.debug(f"Sending direct message: {source} {message} {event}")
        user_id = UserId.create_hashed_user_id(source, self.config.domain)
        client = await self.get_client(user_id, self.config.user_password)
        for room in client.rooms.values():
            self.log_room(room)
            if not room.is_group:
                continue
            if room.group_name() == str(self.admin_user_id.name):
                break
        else:
            raise Exception(f"Source not found: {source}")
        resp = await client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message, "meshcore_event": json.dumps(event, cls=JSONEncoder)},
        )
        if isinstance(resp, RoomSendError):
            raise Exception(str(resp.status_code))

    async def send_message(
        self, source: UserName, message: Message, event: Event, *, channel_name: Optional[ChannelName] = None
    ) -> None:
        if channel_name is None:
            await self.send_direct_message(source, message, event)
            return
        assert channel_name is not None

        user_id = UserId.create_hashed_user_id(source, self.config.domain)

        try:
            client = await self.get_client(user_id, self.config.user_password)
        except Exception as e:
            logging.error(e)
            admin_client = await self.get_client(self.admin_user_id, self.config.admin_password)
            await self.create_user(SecretText(admin_client.access_token), user_id, source, self.config.user_password)
            client = await self.get_client(user_id, self.config.user_password)

        room = next(filter(lambda x: x.name == str(channel_name), client.rooms.values()), None)
        logging.debug(f"Sending message to: {channel_name}")
        if room is None:
            logging.debug(f"Creating new room: {user_id} {channel_name}")
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
            resp = await self.admin_client.room_invite(room_id, str(user_id))
            if isinstance(resp, RoomInviteError):
                # strangely invite returns this if you do it twice for the same user
                if resp.status_code != "M_FORBIDDEN":
                    raise Exception(str(resp.status_code))
            resp = await client.join(room_id)
            if isinstance(resp, JoinError):
                raise Exception(str(resp.status_code))
        else:
            room_id = room.room_id

        resp = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message, "meshcore_event": json.dumps(event, cls=JSONEncoder)},
        )
        if isinstance(resp, RoomSendError):
            raise Exception(str(resp.status_code))

    async def login_user(self, user_id: UserId, user_password: SecretText) -> AsyncClient:
        logging.debug(f"Login user: {user_id} @ {self.config.homeserver}")
        client = AsyncClient(self.config.homeserver, str(user_id))
        resp = await client.login(user_password.raw)
        if isinstance(resp, LoginError):
            await client.close()
            raise Exception(f"Login failed: {resp}")
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

    async def join_user(self, admin_token: SecretText, channel_name: ChannelName, user_id: UserId):
        url = f"{self.config.homeserver}/_synapse/admin/v1/join/Public"
        payload = {
            "user_id": str(user_id),
        }
        headers = {"Authorization": f"Bearer {admin_token.raw}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200 or resp.status == 201:
                    logging.debug(f"User joined: {user_id}")
                else:
                    text = await resp.text()
                    raise Exception(text)

    async def create_user(
        self, admin_token: SecretText, user_id: UserId, display_name: UserName, user_password: SecretText
    ):
        url = f"{self.config.homeserver}/_synapse/admin/v2/users/{user_id}"
        payload = {
            "password": user_password.raw,
            "admin": False,
            "deactivated": False,
            "displayname": str(display_name),
        }
        headers = {"Authorization": f"Bearer {admin_token.raw}", "Content-Type": "application/json"}

        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=payload) as resp:
                if resp.status == 200 or resp.status == 201:
                    logging.debug(f"User created: {user_id}")
                else:
                    text = await resp.text()
                    raise Exception(f"Failed ({resp.status}): {text}")
