import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web
from aiohttp.web_request import Request

from mesh2irc.matrix.common import RoomAlias, RoomId, UserId, RoomName

AS_TOKEN = "AS_SUPER_SECRET_TOKEN"
HS_TOKEN = "HS_SUPER_SECRET_TOKEN"
HOMESERVER = "http://polaris:8008"


class MatrixASChatter:

    def __init__(self):
        self.as_token = AS_TOKEN

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


async def verify_as_token(request: Request):
    token = request.headers.get("Authorization")
    if token != f"Bearer {HS_TOKEN}":
        raise web.HTTPUnauthorized()


async def transactions(request: Request):
    try:
        print(request)
        print(type(request))
        print(request.query)
        # code.interact(local=locals())
        await verify_as_token(request)
        print("XXX")

        # txn_id = request.match_info["txn_id"]
        payload = await request.json()
        print(payload)

        events = payload.get("events", [])
        for event in events:
            if event["type"] == "m.room.message":
                user_id = UserId.parse_user_id(event["sender"])
                body = event["content"].get("body")
                room_id = RoomId(event["room_id"])
                # bleh = "@t_82bf1a1956ca8f370b7903f94e8b3d97b26f325f2f89c05e08baaf5e8e7a4adb:example.com"
                # bleh = "@turd:example.com"
                # await send_as(bleh, room_id, "booga")
                print(await get_room_name(room_id, user_id))
                print(json.dumps(await get_room_state(room_id, user_id)))

                print(f"[{room_id}] {user_id}: {body}")

        return web.json_response({})
    except Exception as e:
        print(e)
    finally:
        print("here")
    return web.json_response({})


async def send_as(user_id: UserId, room_id: RoomId, body: str):
    txn_id = str(time.time())

    headers = {
        "Authorization": f"Bearer {AS_TOKEN}",
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


async def get_room_name(room_id: RoomId, user_id: UserId):
    headers = {
        "Authorization": f"Bearer {AS_TOKEN}",
    }
    params = {
        "user_id": str(user_id),
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(
            f"{HOMESERVER}/_matrix/client/v3/rooms/{room_id}/state/m.room.name",
            params=params,
        ) as resp:
            if resp.status != 200:
                raise Exception(await resp.text())
            return RoomName((await resp.json())["name"])


async def users(request: Request):
    print(request)
    logging.error("DREW")
    # await verify_as_token(request)
    return web.json_response({})


async def rooms(request: Request):
    print(request)
    await verify_as_token(request)
    return web.json_response({})


app = web.Application()
app.router.add_put(
    "/_matrix/app/v1/transactions/{txn_id}",
    transactions,
)
app.router.add_get(
    "/_matrix/app/v1/users/{user_id}",
    users,
)
app.router.add_get(
    "/_matrix/app/v1/rooms/{alias}",
    rooms,
)


async def amain():
    mas_chatter = MatrixASChatter()
    # await mas_chatter.create_room(RoomName("plop"), [])
    print(await mas_chatter.get_room_id_by_alias(RoomAlias("#plop:example.com")))
    return

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host="0.0.0.0", port=9000)
    await site.start()

    await asyncio.Event().wait()

    # return runner  # keep this so you can cleanly shut down later


def main():
    # logging.basicConfig(level=logging.DEBUG)
    # asyncio.run(amain())
    web.run_app(app, host="0.0.0.0", port=9000)
