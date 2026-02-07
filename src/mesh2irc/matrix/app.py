import asyncio
import dataclasses
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
from aiohttp import web
from aiohttp.web_request import Request
from meshcore.events import Event

from mesh2irc.chatter import DirectCallback, ChannelCallback
from mesh2irc.common import ContactName, Message, ChannelName, Contact
from mesh2irc.matrix.common import RoomAlias, RoomId, UserId, RoomName, SecretText
from mesh2irc.matrix.config import Config

AS_TOKEN = "AS_SUPER_SECRET_TOKEN"
HS_TOKEN = "HS_SUPER_SECRET_TOKEN"
HOMESERVER = "http://localhost:8008"


@dataclasses.dataclass(frozen=True)
class Room:
    room_id: RoomId
    room_name: Optional[RoomName]
    is_direct: bool


class MatrixASChatter:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.as_token = AS_TOKEN
        self.channel_callbacks = list[ChannelCallback]()
        self.room_cache = dict[RoomId, Room]()

    async def init(self):
        pass

    async def run(self):
        app = web.Application()
        app.router.add_put(
            "/_matrix/app/v1/transactions/{txn_id}",
            self.transactions,
        )
        app.router.add_get(
            "/_matrix/app/v1/users/{user_id}",
            users,
        )
        app.router.add_get(
            "/_matrix/app/v1/rooms/{alias}",
            rooms,
        )

        runner = web.AppRunner(app)
        await runner.setup()

        site = web.TCPSite(runner, host="0.0.0.0", port=9000)
        await site.start()

    async def update_contact(self, contact: Contact) -> None:
        pass

    async def send_direct(self, source: Contact, message: Message, event: Event) -> None:
        pass

    async def send_channel(
        self, source: ContactName, message: Message, event: Event, channel_name: ChannelName
    ) -> None:
        pass

    async def add_direct_callback(self, cb: DirectCallback) -> None:
        pass

    async def add_channel_callback(self, cb: ChannelCallback) -> None:
        self.channel_callbacks.append(cb)

    async def create_room(self, name: RoomName, invite: list[UserId]):
        headers = {
            "Authorization": f"Bearer {self.as_token}",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "name": name,
            "room_alias_name": name,
            "visibility": "public",  # "private" or "public"
            "invite": [str(e) for e in invite],
            "preset": "public_chat",  # default preset
            "creation_content": {},  # optional, e.g., {"type": "m.space"}
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                f"{HOMESERVER}/_matrix/client/v3/createRoom",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Failed to create room: {resp.status} {text}")
                data = await resp.json()
                return data["room_id"]

    async def get_room_id_by_alias(self, room_alias: RoomAlias):
        headers = {
            "Authorization": f"Bearer {self.as_token}",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            url = f"{HOMESERVER}/_matrix/client/v3/directory/room/{quote(room_alias)}"
            print(url)
            async with session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Failed to lookup room alias: {resp.status} {text}")
                data = await resp.json()
                return data["room_id"]

    async def get_room(self, room_id: RoomId, *, joined_user_id: UserId):
        if room_id in self.room_cache:
            return self.room_cache[room_id]
        headers = {
            "Authorization": f"Bearer {self.as_token}",
        }
        params = {
            "user_id": str(joined_user_id),
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                f"{HOMESERVER}/_matrix/client/v3/rooms/{room_id}/state/m.room.name",
                params=params,
            ) as resp:
                if resp.status != 200:
                    raise Exception(await resp.text())
                json_data = await resp.json()
                name = RoomName(json_data["name"])
                print(json_data)

        room = Room(room_id, name, False)
        self.room_cache[room_id] = room
        return room

    async def send_as(self, user_id: UserId, room_id: RoomId, body: str):
        txn_id = str(time.time())

        headers = {
            "Authorization": f"Bearer {self.as_token}",
        }

        params = {
            "user_id": str(user_id),
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.put(
                f"{HOMESERVER}/_matrix/client/v3/rooms/{room_id}/send/" f"m.room.message/{txn_id}",
                params=params,
                json={
                    "msgtype": "m.text",
                    "body": body,
                },
            ) as resp:
                if resp.status != 200:
                    print(await resp.text())

    # TODO how to know if it is a direct message
    async def handle_room_message(self, event: dict[str, Any]) -> None:
        event_id = event["event_id"]
        user_id = UserId.parse_user_id(event["sender"])
        room_id = RoomId(event["room_id"])
        room_name = await self.get_room(room_id, joined_user_id=user_id)
        channel_name = ChannelName(room_name)
        body = Message(event["content"]["body"])
        for cb in self.channel_callbacks:
            cb(channel_name, body, event_id)

    async def transactions(self, request: Request):
        try:
            print(request)
            print(type(request))
            print(request.query)
            # code.interact(local=locals())
            await verify_as_token(request)
            print("XXX")

            # txn_id = request.match_info["txn_id"]
            payload = await request.json()
            print(json.dumps(payload))

            events = payload.get("events", [])
            for event in events:
                if event["type"] == "m.room.message":
                    print("A")
                    await self.handle_room_message(event)
                    print("B")
                    # user_id = UserId.parse_user_id(event["sender"])
                    # body = event["content"].get("body")
                    # room_id = RoomId(event["room_id"])
                    # # bleh = "@t_82bf1a1956ca8f370b7903f94e8b3d97b26f325f2f89c05e08baaf5e8e7a4adb:example.com"
                    # # bleh = "@turd:example.com"
                    # # await send_as(bleh, room_id, "booga")
                    # print(await get_room_name(room_id, user_id))
                    # print(json.dumps(await get_room_state(room_id, user_id)))
                    #
                    # print(f"[{room_id}] {user_id}: {body}")

            # return web.json_response({})
        except Exception as e:
            print(e)
            raise e
        finally:
            print("here")
        return web.json_response({})

    async def join_room(
        self,
        room_id: str,
    ):

        servers = "&".join(f"server_name={s}" for s in ["example.com"])

        url = f"{HOMESERVER}/_matrix/client/v3/join/" + quote(f"{room_id}?{servers}&user_id=@examplebot:example.com")

        # url = f"{HOMESERVER}/_matrix/client/v3/join/{room_id}"
        headers = {"Authorization": f"Bearer {self.as_token}"}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise Exception(f"Failed to join room: {resp.status} {text}")
                return resp.status, text

    async def invite_user(
        self,
        room_id: RoomId,
        user_id: UserId,
    ):
        url = f"{HOMESERVER}/_matrix/client/v3/rooms/{room_id}/invite"

        headers = {
            "Authorization": f"Bearer {self.as_token}",
            "Content-Type": "application/json",
        }

        payload = {"user_id": str(user_id)}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                text = await resp.text()

                if resp.status == 200:
                    return {
                        "success": True,
                        "status": resp.status,
                    }

                raise Exception(f"Failed to invite user: {resp.status} {text}")
                # return {
                #     "success": False,
                #     "status": resp.status,
                #     "error": text,
                # }


async def verify_as_token(request: Request):
    token = request.headers.get("Authorization")
    if token != f"Bearer {HS_TOKEN}":
        raise web.HTTPUnauthorized()


async def get_room_state(room_id: RoomId, user_id: UserId):
    headers = {
        "Authorization": f"Bearer {AS_TOKEN}",
    }
    params = {
        "user_id": str(user_id),
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(
            f"{HOMESERVER}/_matrix/client/v3/rooms/{room_id}/state/m.room.create",
            params=params,
        ) as resp:
            if resp.status != 200:
                raise Exception(await resp.text())
            return await resp.json()


async def users(request: Request):
    print(request)
    logging.error("DREW")
    # await verify_as_token(request)
    return web.json_response({})


async def rooms(request: Request):
    print(request)
    await verify_as_token(request)
    return web.json_response({})


#
# app = web.Application()
# app.router.add_put(
#     "/_matrix/app/v1/transactions/{txn_id}",
#     transactions,
# )
# app.router.add_get(
#     "/_matrix/app/v1/users/{user_id}",
#     users,
# )
# app.router.add_get(
#     "/_matrix/app/v1/rooms/{alias}",
#     rooms,
# )


async def amain():
    mas_chatter = MatrixASChatter()
    # await mas_chatter.create_room(RoomName("plop"), [])
    # print(await mas_chatter.get_room_id_by_alias(RoomAlias("#plop:example.com")))
    # return
    # config = Config(
    #     domain=DomainName("example.com"), admin_user=UserName("drew"), admin_password=SecretText("usgp3140")
    # )
    # m_chatter = MatrixChatter(config)
    # await m_chatter.init()
    user_id = UserId.parse_user_id("@t_707b87db78d48f634da6b190cd5d697881a91339f9f0b2608138d1c4755d7b67:example.com")
    user_password = SecretText("password")
    # client = await m_chatter.get_client(user_id, user_password)

    # app = web.Application()
    # app.router.add_put(
    #     "/_matrix/app/v1/transactions/{txn_id}",
    #     mas_chatter.transactions,
    # )
    # app.router.add_get(
    #     "/_matrix/app/v1/users/{user_id}",
    #     users,
    # )
    # app.router.add_get(
    #     "/_matrix/app/v1/rooms/{alias}",
    #     rooms,
    # )
    #
    # runner = web.AppRunner(app)
    # await runner.setup()
    #
    # site = web.TCPSite(runner, host="0.0.0.0", port=9000)
    # await site.start()

    room_id = await mas_chatter.get_room_id_by_alias(RoomAlias("#public:example.com"))
    # print(room_id)
    # user_id = UserId(UserName("drew"), DomainName("example.com"))
    # await mas_chatter.join_room(room_id)
    # await mas_chatter.invite_user(room_id, user_id)
    # await mas_chatter.send_as(user_id, room_id, "boo")
    user_id = UserId.parse_user_id("@t_707b87db78d48f634da6b190cd5d697881a91339f9f0b2608138d1c4755d7b67:example.com")
    # room_id = RoomId("!btJaPFgPSZNhrItEHh:example.com")
    # await client.join(room_id)
    await mas_chatter.send_as(user_id, room_id, "hello")

    await asyncio.Event().wait()

    # return runner  # keep this so you can cleanly shut down later


def main():
    # logging.basicConfig(level=logging.DEBUG)
    asyncio.run(amain())
    # web.run_app(app, host="0.0.0.0", port=9000)
