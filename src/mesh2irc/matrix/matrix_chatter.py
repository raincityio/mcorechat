#!/usr/bin/env python3
import asyncio
import logging
from typing import Optional

from nio import RoomVisibility
from nio.client import AsyncClient

from mesh2irc.chatter import ChannelName, Message, UserName
from mesh2irc.matrix.common import UserId
from mesh2irc.matrix.config import Config
from mesh2irc.matrix.new_user import create_user


async def login_user(config: Config, user_id: UserId, user_password: str) -> AsyncClient:
    # user_id = f"@{admin_user_name}:{config.domain}"
    logging.debug(f"Login user: {user_id} @ {config.homeserver}")
    client = AsyncClient(config.homeserver, str(user_id))
    resp = await client.login(user_password)
    if hasattr(resp, "access_token") and resp.access_token:
        print(f"Logged in! Access token: {resp.access_token}")
    else:
        raise Exception(f"Login failed: {resp}")
    # return resp.access_token
    return client


class MatrixChatter:
    def __init__(self, config: Config):
        self.config = config
        self.clients = dict[UserId, AsyncClient]()
        self.clients_lock = asyncio.Lock()
        self.admin_user_id = UserId(config.admin_user, config.domain)

    async def get_room(self, room_name: str):
        admin_client = await self.get_client(self.admin_user_id, self.config.admin_password)
        await admin_client.sync()
        room = next(filter(lambda room: room.name == room_name, admin_client.rooms.values()))
        return room

    async def send_message(
        self, source: UserName, message: Message, *, channel_name: Optional[ChannelName] = None
    ) -> None:
        assert channel_name is not None

        user_id = UserId.create(source, self.config.domain)

        try:
            client = await self.get_client(user_id, "password")
        except Exception as e:
            logging.error(e)
            admin_client = await self.get_client(self.admin_user_id, self.config.admin_password)
            await create_user(self.config, admin_client.access_token, user_id, source, "password")
            client = await self.get_client(user_id, "password")

        room = next(filter(lambda x: x.name == channel_name, client.rooms.values()), None)
        if room is None:
            admin_client = await self.get_client(self.admin_user_id, self.config.admin_password)
            await admin_client.sync()
            resp = await admin_client.room_create(
                visibility=RoomVisibility.public, alias=channel_name, name=channel_name, topic=channel_name
            )
            if hasattr(resp, "status_code"):
                if resp.status_code != "M_ROOM_IN_USE":
                    raise Exception(str(resp.status_code))
            await admin_client.sync()
            room = next(filter(lambda x: x.name == channel_name, admin_client.rooms.values()), None)
            room_id = room.room_id
            resp = await admin_client.room_invite(room_id, str(user_id))
            if hasattr(resp, "status_code"):
                if resp.status_code != "M_FORBIDDEN":
                    raise Exception(str(resp.status_code))
            resp = await client.join(room_id)
            if hasattr(resp, "status_code"):
                raise Exception(str(resp.status_code))
        else:
            room_id = room.room_id

        await client.room_send(
            room_id=room_id, message_type="m.room.message", content={"msgtype": "m.text", "body": message}
        )

    async def get_client(self, user_id: UserId, password: str) -> AsyncClient:
        client = self.clients.get(user_id, None)
        if client is None:
            new_clients = self.clients.copy()
            client = await login_user(self.config, user_id, password)
            await client.sync()
            new_clients[user_id] = client
            self.clients = new_clients
        return client

    # async def get_identity(self, user_name: UserName) -> Identity:
    #     user_id = UserId.create(user_name, self.config.domain)
    #     client = self.clients.get(user_id, None)
    #     if client is None:
    #         async with self.clients_lock:
    #             new_clients = self.clients.copy()
    #             new_clients[user_id] = await self.get_client(user_id)
    #             self.clients = new_clients
    #     return user_id
