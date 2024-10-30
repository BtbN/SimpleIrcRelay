#!/usr/bin/env python3

from aiohttp import web

import irc.client
import irc.client_aio

import functools
import asyncio
import sys
import os


class AioSimpleIRCClient(irc.client_aio.AioSimpleIRCClient):
    def __init__(self, channel, http_host, http_port):
        super().__init__()
        self.channel = channel

        self.http_host = http_host
        self.http_port = http_port
        self.is_setup = False

        self.future = None

    def on_welcome(self, con, event):
        print("Connected!")
        con.join(self.channel)

    def on_join(self, con, event):
        print("Joined!")
        self.future = asyncio.ensure_future(self.setup_server(), loop=con.reactor.loop)

    def on_disconnect(self, con, event):
        print("Disconnected")
        if self.future:
            self.future.cancel()
        self.future = asyncio.ensure_future(self.cleanup_self(), loop=con.reactor.loop)

    async def cleanup_self(self):
        if not self.is_setup:
            return
        await self.runner.cleanup()
        asyncio.get_running_loop().stop()

    async def setup_server(self):
        if self.is_setup:
            return

        try:
            self.app = web.Application()
            self.app.router.add_put("/message", self.sendmsg)

            print("Setting up runner...")

            self.runner = web.AppRunner(self.app)
            await self.runner.setup()

            print("Setting up site...")

            self.site = web.TCPSite(self.runner, self.http_host, self.http_port)
            await self.site.start()

            print("Setup done")
            self.is_setup = True
        except Exception as e:
            print(e)
            sys.exit(1)

    async def sendmsg(self, req):
        msg = await req.text()
        print("Posting message: " + msg)
        self.connection.privmsg(self.channel, msg)
        return web.Response()


def bot_main():
    server = os.getenv("IRC_SERVER", "irc.libera.chat")
    port = os.getenv("IRC_PORT", "+6697")
    channel = os.getenv("IRC_CHANNEL", "#testmybot")
    nick = os.getenv("IRC_NICK", "FFTrac")

    ssl = port.startswith("+")
    port = int(port.replace("+", ""))

    http_host = os.getenv("IRC_HTTP_HOST", "localhost")
    http_port = int(os.getenv("IRC_HTTP_PORT", "8080"))

    client = AioSimpleIRCClient(channel, http_host, http_port)

    try:
        client.connect(server, port, nick, connect_factory=irc.connection.AioFactory(ssl=ssl))
    except irc.client.ServerConnectionError as e:
        print(e)
        return 1

    try:
        client.start()
    finally:
        client.connection.disconnect()
        if client.future:
            client.reactor.loop.run_until_complete(client.future)
        client.reactor.loop.close()

    return 0


if __name__ == "__main__":
    sys.exit(bot_main())
