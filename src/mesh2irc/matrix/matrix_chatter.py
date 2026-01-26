#!/usr/bin/env python3
import asyncio
import json
import logging
from typing import Optional

import aiohttp
from meshcore.events import Event
from nio import (
    JoinError,
    LoginError,
    RoomCreateError,
    RoomInviteError,
    RoomSendError,
    RoomVisibility,
)
from nio.client import AsyncClient

from mesh2irc.chatter import ChannelName, Message, UserName
from mesh2irc.common import JSONEncoder
from mesh2irc.matrix.common import UserId, SecretText
from mesh2irc.matrix.config import Config


class MatrixChatter:
    def __init__(self, config: Config):
        self.config = config
        self.clients = dict[UserId, AsyncClient]()
        self.clients_lock = asyncio.Lock()
        self.admin_user_id = UserId(config.admin_user, config.domain)

    async def send_message(
        self, source: UserName, message: Message, event: Event, *, channel_name: Optional[ChannelName] = None
    ) -> None:
        assert channel_name is not None

        user_id = UserId.create_hashed_user_id(source, self.config.domain)

        try:
            client = await self.get_client(user_id, self.config.user_password)
        except Exception as e:
            logging.error(e)
            admin_client = await self.get_client(self.admin_user_id, self.config.admin_password)
            await self.create_user(SecretText(admin_client.access_token), user_id, source, self.config.user_password)
            client = await self.get_client(user_id, self.config.user_password)

        room = next(filter(lambda x: x.name == channel_name, client.rooms.values()), None)
        if room is None:
            admin_client = await self.get_client(self.admin_user_id, self.config.admin_password)
            resp = await admin_client.room_create(
                visibility=RoomVisibility.public, alias=channel_name, name=channel_name, topic=channel_name
            )
            if isinstance(resp, RoomCreateError):
                if resp.status_code != "M_ROOM_IN_USE":
                    raise Exception(str(resp.status_code))
            # await admin_client.sync()
            room = next(filter(lambda x: x.name == channel_name, admin_client.rooms.values()))
            room_id = room.room_id
            resp = await admin_client.room_invite(room_id, str(user_id))
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
        return client

    async def create_user(
        self, admin_token: SecretText, user_id: UserId, display_name: UserName, user_password: SecretText
    ):
        url = f"{self.config.homeserver}/_synapse/admin/v2/users/{user_id}"
        payload = {"password": user_password.raw, "admin": False, "deactivated": False, "displayname": display_name}
        headers = {"Authorization": f"Bearer {admin_token.raw}", "Content-Type": "application/json"}

        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=payload) as resp:
                if resp.status == 200 or resp.status == 201:
                    logging.debug(f"User created: {user_id}")
                else:
                    text = await resp.text()
                    raise Exception(f"Failed ({resp.status}): {text}")
