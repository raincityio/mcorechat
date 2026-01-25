#!/usr/bin/env python3

import asyncio
import dataclasses
import enum
import logging
from asyncio.streams import StreamReader, StreamWriter
from typing import Optional

import pydle.features.rfc1459


class RPL(enum.Enum):
    WELCOME = 1
    YOURHOST = 2
    CREATED = 3
    MYINFO = 4

    LISTSTART = 321  # not used
    LIST = 322
    LISTEND = 323

    TOPIC = 332
    NAMREPLY = 353
    ENDOFNAMES = 366

    RPL_MOTDSTART = 375
    RPL_MOTD = 372
    RPL_ENDOFMOTD = 376


@dataclasses.dataclass(frozen=True)
class Identity:
    # user: str
    nick: str


@dataclasses.dataclass(frozen=True)
class IRCClient:
    user: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    wlock: asyncio.Lock = asyncio.Lock()


class Channel:
    def __init__(self, name: str, *, topic: Optional[str] = None):
        self.name = name
        self.topic = name if topic is None else topic
        self.clients = set[IRCClient]()

    def get_users_count(self):
        return len(self.clients)

    def list_clients(self):
        return self.clients.copy()

    def add_client(self, client: IRCClient):
        if client in self.clients:
            return False
        self.clients.add(client)
        return True

    def remove_client(self, client: IRCClient):
        if client not in self.clients:
            return False
        self.clients.remove(client)
        return True


class IRCServer:
    def __init__(self):
        self.channels = {"#public": Channel("#public"), "#poop": Channel("#poops")}
        self.identities = dict[IRCClient, Identity]()
        self.clients = set[IRCClient]()

    def add_client(self, client: IRCClient, identity: Identity):
        self.identities[client] = identity
        self.clients.add(client)

    def remove_client(self, client: IRCClient):
        self.clients.remove(client)
        self.identities.pop(client)
        for channel in self.channels.values():
            channel.remove_client(client)

    def get_identity(self, client: IRCClient):
        return self.identities[client]

    def get_channel(self, name: str) -> Channel:
        if name not in self.channels:
            self.channels[name] = Channel(name)
        return self.channels[name]

    def list_channels(self):
        for channel in self.channels.values():
            yield channel


async def send_nreply(server: IRCServer, client: IRCClient, rpl: RPL, text: str):
    identity = server.get_identity(client)
    async with client.wlock:
        raw_text = f"{str(rpl.value).zfill(3)} {identity.nick} {text}\r\n"
        logging.debug(f"send_nreply: {raw_text[:-2]}")
        client.writer.write(raw_text.encode())


async def send_text(server: IRCServer, client: IRCClient, text: str, *, source: Optional[IRCClient] = None):
    source = client if source is None else source
    source_identity = server.get_identity(source)
    async with client.wlock:
        raw_text = f":{source_identity.nick}!{source.user}@localhost {text}\r\n"
        logging.debug(f"send_text: {raw_text[:-2]}")
        client.writer.write(raw_text.encode())


@dataclasses.dataclass(frozen=True)
class Command:
    source: Optional[str]
    command: str
    params: Optional[list[str]]

    @staticmethod
    def parse_from(text: bytes):
        parsed = pydle.features.rfc1459.RFC1459Message.parse(text)
        return Command(parsed.source, parsed.command, parsed.params)


async def read_command(reader: StreamReader):
    command_line = await reader.readuntil(b"\r\n")
    command = Command.parse_from(command_line)
    logging.debug(command)
    return command


async def irc_list(server: IRCServer, client: IRCClient):
    await send_nreply(server, client, RPL.LISTSTART, ":list start")
    for channel in server.list_channels():
        await send_nreply(server, client, RPL.LIST, f"{channel.name} {channel.get_users_count()} :{channel.topic}")
    await send_nreply(server, client, RPL.LISTEND, ":list end")


async def join(server: IRCServer, client: IRCClient, cmd: Command):
    identity = server.get_identity(client)
    assert cmd.params is not None
    channel_names = cmd.params[0].split(",")
    for channel_name in channel_names:
        channel = server.get_channel(channel_name)
        added = channel.add_client(client)
        assert added
        await send_text(server, client, f"JOIN {channel.name}")
        await send_nreply(server, client, RPL.TOPIC, f"{channel.name} {channel.topic}")
        for client in channel.list_clients():
            await send_nreply(server, client, RPL.NAMREPLY, f"= {channel.name} {identity.nick}")
        await send_nreply(server, client, RPL.ENDOFNAMES, f"{channel.name} :end of names")


async def pong(server: IRCServer, client: IRCClient, cmd: Command):
    assert cmd.params is not None
    to_pong = " ".join(cmd.params)
    await send_text(server, client, f"PONG {to_pong}")


async def mode(server: IRCServer, client: IRCClient, cmd: Command):
    assert cmd.params is not None
    modes = cmd.params[0]
    # pydle.features.rfc1459.RFC1459Message.parse_modes()


async def privmsg(server: IRCServer, client: IRCClient, cmd: Command):
    assert cmd.params is not None
    target = cmd.params[0]
    message = cmd.params[1]
    if target.startswith("#"):
        channel = server.get_channel(target)
        for channel_client in channel.list_clients():
            if channel_client == client:
                continue
            await send_text(server, channel_client, f"PRIVMSG {target} :{message}", source=client)
    else:
        raise Exception(f"Unknown target: {target}")


async def amain():
    logging.basicConfig(level=logging.DEBUG)
    irc_server = IRCServer()

    async def cb(reader: StreamReader, writer: StreamWriter):
        next_cmd = await read_command(reader)
        if next_cmd.command == "CAP":
            next_cmd = await read_command(reader)
        if next_cmd.command == "PASS":
            nick_cmd = await read_command(reader)
        else:
            nick_cmd = next_cmd
        assert nick_cmd.command == "NICK"
        assert nick_cmd.params is not None
        user_cmd = await read_command(reader)
        assert user_cmd.command == "USER"
        assert user_cmd.params is not None

        client = IRCClient(user_cmd.params[0], reader=reader, writer=writer)
        irc_server.add_client(client, Identity(nick=nick_cmd.params[0]))
        try:
            # (nick=nick_cmd.subcommands[0], user=user_cmd.subcommands[0]))
            await send_nreply(irc_server, client, RPL.WELCOME, ":Welcome to the IRC server!")
            await send_nreply(irc_server, client, RPL.YOURHOST, ":Your host is something")
            await send_nreply(irc_server, client, RPL.CREATED, ":Created today")
            await send_nreply(irc_server, client, RPL.MYINFO, ":meserver meversion")

            # (Reply.Welcome, f":Welcome, {client_data.id()}"),
            # (Reply.YourHost, f":Your host is {self.server_name}, running version {self.version}"),
            # (Reply.Created, ":This server was created today"),
            # (Reply.MyInfo, f"{self.server_name} {self.version}  "),
            # (Reply.ISupport, f"NETWORK={self.network_name} :are supported by this server"),

            await send_nreply(irc_server, client, RPL.RPL_MOTDSTART, ":hello")
            await send_nreply(irc_server, client, RPL.RPL_MOTD, ":another")
            await send_nreply(irc_server, client, RPL.RPL_ENDOFMOTD, ":world")

            while True:
                #     await send_nreply(client, writer, RPL.RPL_MOTDSTART)
                cmd = await read_command(reader)
                if cmd.command == "LIST":
                    await irc_list(irc_server, client)
                elif cmd.command == "JOIN":
                    await join(irc_server, client, cmd)
                elif cmd.command == "PING":
                    await pong(irc_server, client, cmd)
                elif cmd.command == "MODE":
                    await mode(irc_server, client, cmd)
                elif cmd.command == "PRIVMSG":
                    await privmsg(irc_server, client, cmd)
                else:
                    print(f"unknown command: {cmd}")
        finally:
            irc_server.remove_client(client)

    server = await asyncio.start_server(cb, "0.0.0.0", 6667)
    await server.serve_forever()


def main():
    asyncio.run(amain())
