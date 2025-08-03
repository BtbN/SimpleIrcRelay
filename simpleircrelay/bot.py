#!/usr/bin/env python3

from aiohttp import web
import aiohttp
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import irc.client
import irc.client_aio

import asyncio
import collections
import html
import inspect
import sys
import os
import re


LINK_WITH_DESC = re.compile(r'<([^|>]+)\|([^>]+)>')
BARE_LINK = re.compile(r'<([^|>]+)>')

def cleanup_slack_msg(text):
    def link(match):
        url, desc = match.group(1), match.group(2)
        if "/src/branch/" in url:
            return desc
        return f"{desc} ({url})"
    text = LINK_WITH_DESC.sub(link, text)
    text = BARE_LINK.sub(r'\1', text)
    text = html.unescape(text)
    return text


def noping(username):
    return f"{username[0:1]}\u200b{username[1:]}"


class AioSimpleIRCClient(irc.client_aio.AioSimpleIRCClient):
    def __init__(self, channel, http_host, http_port):
        super().__init__()
        self.channel = channel

        self.http_host = http_host
        self.http_port = http_port
        self.is_setup = False

        self.future = None

        self.pr_merge_sha_cache = collections.deque(maxlen=10)

    def on_welcome(self, con, event):
        print("Connected!")
        con.join(self.channel)

    def on_join(self, con, event):
        print("Joined!")
        self.future = asyncio.ensure_future(self.setup_server(), loop=con.reactor.loop)

    def on_disconnect(self, con, event):
        print("Disconnected")
        sys.exit(0)

    async def setup_server(self):
        if self.is_setup:
            return

        try:
            self.app = web.Application()
            self.app.router.add_put("/message", self.sendmsg)
            self.app.router.add_post("/slack", self.sendslackmsg)
            self.app.router.add_post("/forgejo", self.sendfjmsg)

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

    def post(self, msg):
        print("Posting message: " + msg)
        self.connection.privmsg(self.channel, msg)

    async def sendmsg(self, req):
        msg = await req.text()
        self.post(msg)
        return web.Response()

    async def sendslackmsg(self, req):
        msg = await req.json()
        text = cleanup_slack_msg(msg["text"])
        self.post(text)
        return web.Response()

    async def sendfjmsg(self, req):
        event_type = req.headers.get("X-Forgejo-Event", "")
        if not event_type:
            event_type = req.headers.get("X-GitHub-Event", "")

        if not event_type:
            raise web.HTTPBadRequest()

        msg = await req.json()
        action = msg.get("action", "")

        if event_type == "pull_request" and action == "opened":
            self.handle_pr_issue_opened(msg, action)
            if "SMTP_HOST" in os.environ:
                asyncio.create_task(self.send_pr_email(msg))
        elif event_type == "pull_request" and action == "reopened":
            self.handle_pr_issue_opened(msg, action)
        elif event_type == "pull_request" and action == "closed":
            self.handle_pr_issue_closed(msg)
        elif event_type == "issue" and action == "opened":
            self.handle_pr_issue_opened(msg, action)
        elif event_type == "issue" and action == "reopened":
            self.handle_pr_issue_opened(msg, action)
        elif event_type == "issue" and action == "closed":
            self.handle_pr_issue_closed(msg)
        elif event_type == "issue_comment" and action == "created":
            self.handle_issue_comment(msg)
        elif event_type == "push":
            self.handle_push(msg)

        return web.Response()

    async def send_pr_email(self, msg):
        try:
            pr = msg['pull_request']
            sender = msg['sender']
            pr_body = pr.get('body', '')
            sender_username = sender['username']
            sender_name = sender.get('full_name', sender_username)
            if not sender_name.strip():
                sender_name = sender_username

            mail_from = os.environ.get("MAIL_FROM", "bot@localhost")
            mail_to = os.environ.get("MAIL_TO", "root@localhost")

            async with aiohttp.ClientSession() as session:
                async with session.get(pr['patch_url']) as response:
                    if response.status == 200:
                        patch_content = await response.text()
                    else:
                        patch_content = f"Could not fetch patch content (HTTP {response.status})"

            msg = MIMEText('', 'plain')
            msg['From'] = f"{sender_name} <{mail_from}>"
            msg['To'] = mail_to
            msg['Subject'] = f"[PATCH] {pr['title']} (PR #{pr['number']})"

            if sender_name != sender_username:
                sender_display = f"{sender_name} ({sender_username})"
            else:
                sender_display = sender_name

            email_body = f"""PR #{pr['number']} opened by {sender_display}
            URL: {pr['html_url']}
            Patch URL: {pr['patch_url']}

            {pr_body if pr_body else ''}
            """
            email_body = f"{inspect.cleandoc(email_body)}\n\n{patch_content}"

            msg.set_payload(email_body)

            await aiosmtplib.send(
                msg,
                hostname=os.getenv("SMTP_HOST", "localhost"),
                port=int(os.getenv("SMTP_PORT", "25")),
                username=os.getenv("SMTP_USER", None),
                password=os.getenv("SMTP_PASS", None),
                use_tls=os.getenv("SMTP_TLS", "true").lower() != "false"
            )

            print(f"Email sent for PR #{pr['number']}")
        except Exception as e:
            print(f"Failed to send email for PR: {e}")

    def handle_pr_issue_opened(self, msg, action):
        obj = msg.get('pull_request', msg.get('issue', {}))
        repo = msg['repository']
        user = msg['sender']
        issue_type = 'Pull request' if msg.get('pull_request', None) else 'Issue'

        text = f"[{repo['full_name']}] {issue_type} #{obj['number']} {action}: {obj['title']} ({obj['html_url']}) by {noping(user['username'])}"
        self.post(text)

    def handle_issue_comment(self, msg):
        issue = msg['issue']
        repo = msg['repository']
        user = issue['sender']
        issue_type = 'pull request' if issue.get('is_pull', False) else 'issue'

        text = f"[{repo['full_name']}] New comment on {issue_type} #{issue['number']} {issue['title']} ({issue['url']}) by {noping(user['username'])}"
        self.post(text)

    def handle_pr_issue_closed(self, msg):
        obj = msg.get('pull_request', msg.get('issue', {}))
        repo = msg['repository']
        user = msg['sender']
        action = 'merged' if obj.get('merged', False) else 'closed'

        if merge_sha := obj.get('merge_commit_sha', ''):
            self.pr_merge_sha_cache.append(merge_sha)

        text = f"[{repo['full_name']}] Pull request #{obj['number']} {action}: {obj['title']} ({obj['html_url']}) by {noping(user['username'])}"
        self.post(text)

    def handle_push(self, msg):
        # Skip PR merges, as we just announced them as such
        if msg['after'] in self.pr_merge_sha_cache:
            return

        ref = msg['ref'].removeprefix('refs/heads/')
        repo = msg['repository']

        text = f"[{repo['full_name']}:{ref}] {len(msg['commits'])} new commit{'s' if len(msg['commits']) > 1 else ''} ({msg['compare_url']}) pushed by {noping(msg['pusher']['username'])}"
        self.post(text)


def bot_main():
    server = os.getenv("IRC_SERVER", "irc.libera.chat")
    port = os.getenv("IRC_PORT", "+6697")
    channel = os.getenv("IRC_CHANNEL", "#testmybot")
    nick = os.getenv("IRC_NICK", "FFTrac")

    ssl = port.startswith("+")
    port = int(port.replace("+", ""))

    http_host = os.getenv("IRC_HTTP_HOST", "localhost")
    http_port = int(os.getenv("IRC_HTTP_PORT", "8787"))

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
