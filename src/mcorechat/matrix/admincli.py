#!/usr/bin/env python3
import asyncio
import json
import logging
from argparse import ArgumentParser

from nio import Any, AsyncClient, LoginError

from mcorechat.matrix.common import HomeserverURL, UserId, DomainName, UserName, SecretText, RoomId
from mcorechat.matrix.matrix_admin_client import MatrixAdminClient

homeserver = HomeserverURL("http://polaris.elisha.raincity.io:8008")


def jout(o: Any):
    print(json.dumps(o, indent=2))


async def amain():
    logging.basicConfig(level=logging.DEBUG)

    parser = ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    subparsers = parser.add_subparsers(dest="command")
    subparser = subparsers.add_parser("delete-room")
    subparser.add_argument("--room-id", required=True)
    subparsers.add_parser("list-rooms")
    subparsers.add_parser("list-users")
    args = parser.parse_args()

    async def get_admin_client():
        user_id = UserId(UserName(args.user), DomainName("example.com"))
        client = AsyncClient(str(homeserver), str(user_id))
        login_resp = await client.login(args.password)
        if type(login_resp) is LoginError:
            raise Exception(login_resp.status_code)
        return MatrixAdminClient(homeserver, SecretText(client.access_token))

    if args.command is None:
        raise Exception("No command specified")
    elif args.command == "delete-room":
        admin_client = await get_admin_client()
        room_id = RoomId(args.room_id)
        jout(await admin_client.delete_room(room_id))
    elif args.command == "list-rooms":
        admin_client = await get_admin_client()
        jout(await admin_client.list_rooms())
    elif args.command == "list-users":
        admin_client = await get_admin_client()
        jout(await admin_client.list_users())
    else:
        raise Exception(f"Unknown command: {args.command}")


def main():
    asyncio.run(amain())
