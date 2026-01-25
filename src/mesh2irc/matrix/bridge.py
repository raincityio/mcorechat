#!/usr/bin/env python3
import asyncio

import nio
from nio.client import AsyncClient


def get_user(domain: str, name: str):
    return f"@{name}:{domain}"


async def find_room(client: AsyncClient, name: str):
    room = next(filter(lambda room: room.name == name, client.rooms.values()))
    return room


async def amain():
    config = Config()
    # 1️⃣ Log in as the inviter
    inviter_user = get_user(config.domain, config.admin_user)
    client = nio.client.AsyncClient(config.homeserver, inviter_user)
    login_resp = await client.login(config.admin_password)

    if hasattr(login_resp, "status_code"):
        raise Exception(f"Login failed: {login_resp.status_code}")

    # response = await client.list_public_rooms()
    #
    # print(response)
    # print(dir(response))
    await client.sync()
    room = await find_room(client, "test")
    print(room.name)

    # 3️⃣ Invite target user
    target_user = get_user(config.domain, "alice")
    invite_resp = await client.room_invite(room.room_id, target_user)
    if hasattr(invite_resp, "status_code"):
        raise Exception(f"Invite failed: {invite_resp.status_code}")

    print(invite_resp)
    print(dir(invite_resp))

    print("HERE")
    resp = await client.room_send(
        room.room_id, message_type="m.room.message", content={"msgtype": "m.text", "body": "this is a message"}
    )
    if hasattr(resp, "status_code"):
        raise Exception(f"Room send failed: {resp.status_code}")
    print(resp)
    print("THERE")

    # # 4️⃣ Send a message
    # send_resp = await client.room_send(
    #     room_id=room_id, message_type="m.room.message", content={"msgtype": "m.text", "body": MESSAGE}
    # )
    # if isinstance(send_resp, RoomSendResponse):
    #     print(f"Message sent: {MESSAGE}")
    # else:
    #     print(f"Failed to send message: {send_resp}")
    #
    # await client.close()


def main():
    asyncio.run(amain())
