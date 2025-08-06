#!/usr/bin/env python3

from aiohttp import web
import aiohttp
import aiosmtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from cachetools import TTLCache

import irc.client
import irc.client_aio

import asyncio
import html
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
    return f"{username[0:1]}\u2060{username[1:]}"


class AioSimpleIRCClient(irc.client_aio.AioSimpleIRCClient):
    def __init__(self, channel, http_host, http_port):
        super().__init__()
        self.channel = channel

        self.http_host = http_host
        self.http_port = http_port
        self.is_setup = False

        self.future = None

        self.pr_merge_sha_cache = TTLCache(maxsize=10, ttl=30)
        self.handled_jobs_cache = TTLCache(maxsize=10000, ttl=60*60*3)
        self.ci_check_tasks = []
        self.ci_check_lock = asyncio.Lock()

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
            self.launch_delayed_ci()
            self.handle_pr_issue_opened(msg, action)
            if "SMTP_HOST" in os.environ:
                asyncio.create_task(self.send_pr_email(msg))
        elif event_type == "pull_request" and action == "reopened":
            self.handle_pr_issue_opened(msg, action)
        elif event_type == "pull_request" and action == "synchronized":
            self.launch_delayed_ci()
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
            self.launch_delayed_ci()
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

            if sender_name != sender_username:
                sender_display = f"{sender_name} ({sender_username})"
            else:
                sender_display = sender_name

            email_body = (
                f"PR #{pr['number']} opened by {sender_display}\n"
                f"URL: {pr['html_url']}\n"
                f"Patch URL: {pr['patch_url']}\n\n"
                f"{pr_body}\n"
                f"{'\n\n' if pr_body else ''}{patch_content}"
            )

            msg = MIMEText(email_body, 'plain', 'utf-8')
            msg['From'] = formataddr((str(Header(sender_name, 'utf-8')), mail_from), 'utf-8')
            msg['To'] = mail_to
            msg['Reply-To'] = formataddr((str(Header(sender_name, 'utf-8')), sender['email']), 'utf-8')
            msg['Subject'] = Header(f"[PATCH] {pr['title']} (PR #{pr['number']})", 'utf-8')

            await aiosmtplib.send(
                msg,
                hostname=os.getenv("SMTP_HOST", "localhost"),
                port=int(os.getenv("SMTP_PORT", "25")),
                username=os.getenv("SMTP_USER", None),
                password=os.getenv("SMTP_PASS", None),
                use_tls=os.getenv("SMTP_SSL", "false").lower() == "true",
                start_tls=(os.getenv("SMTP_TLS", "true").lower() != "false") or None
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
        user = msg['sender']
        issue_type = 'pull request' if msg.get('is_pull', False) else 'issue'

        text = f"[{repo['full_name']}] New comment on {issue_type} #{issue['number']} {issue['title']} ({msg['comment']['html_url']}) by {noping(user['username'])}"
        self.post(text)

    def handle_pr_issue_closed(self, msg):
        obj = msg.get('pull_request', msg.get('issue', {}))
        repo = msg['repository']
        user = msg['sender']
        action = 'merged' if obj.get('merged', False) else 'closed'

        if merge_sha := obj.get('merge_commit_sha', ''):
            self.pr_merge_sha_cache[merge_sha] = True

        text = f"[{repo['full_name']}] Pull request #{obj['number']} {action}: {obj['title']} ({obj['html_url']}) by {noping(user['username'])}"
        self.post(text)

    def handle_push(self, msg):
        # Skip PR merges, as we just announced them as such
        if msg['after'] in self.pr_merge_sha_cache:
            return

        ref = msg['ref'].removeprefix('refs/heads/')
        repo = msg['repository']

        text = f"[{repo['full_name']}:{ref}] {msg['total_commits']} new commit{'s' if int(msg['total_commits']) > 1 else ''} ({msg['compare_url']}) pushed by {noping(msg['pusher']['username'])}"
        self.post(text)

    def launch_delayed_ci(self):
        self.ci_check_tasks = [task for task in self.ci_check_tasks if not task.done()]
        if len(self.ci_check_tasks) > 1:
            self.ci_check_tasks.pop().cancel()
        self.ci_check_tasks.append(asyncio.create_task(self.delayed_ci_check()))

    async def delayed_ci_check(self):
        try:
            await asyncio.sleep(20)
            async with self.ci_check_lock:
                await self.check_trigger_ci()
        finally:
            ct = asyncio.current_task()
            if ct in self.ci_check_tasks:
                self.ci_check_tasks.remove(ct)

    async def check_trigger_ci(self):
        GHTOKEN=os.getenv("GHCITOKEN", None)
        GHREPO=os.getenv("GHREPO", "BtbN/FFmpeg-CI")
        GHCIID=os.getenv("GHCID", "ci.yml")
        GHAPIURL=os.getenv("GHAPIURL", "https://api.github.com")
        FJTOKEN=os.getenv("FJCITOKEN", None)
        FJLABELS=os.getenv("FJLABELS", "linux-aarch64:aarch64_count,linux-amd64:x86_64_count").split(",")
        FJACTID=os.getenv("FJACTID", "orgs/FFmpeg")
        FJURL=os.getenv("FJURL", "http://forgejo:3000")

        if not GHTOKEN or not FJTOKEN:
            return

        FJLABELMAP = {}
        for label in FJLABELS:
            val = label.split(':', 1)
            FJLABELMAP[val[0]] = val[1] if len(val) > 1 else val[0]

        CICOUNTS={}
        LAUNCHEDJOBS = set()
        async with aiohttp.ClientSession() as session:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {FJTOKEN}"
            }
            async with session.get(f"{FJURL}/api/v1/{FJACTID}/actions/runners/jobs?labels={','.join(FJLABELMAP.keys())}", headers=headers) as response:
                if response.status != 200:
                    print(f"Failed to fetch Forgejo runners: {response.status}")
                    return
                data = await response.json()
                if not data or len(data) == 0:
                    print("No jobs to check.")
                    return
                for job in data:
                    if job['status'] != 'waiting' or job['id'] in self.handled_jobs_cache:
                        continue
                    for label in job['runs_on']:
                        if label in FJLABELMAP:
                            CICOUNTS[FJLABELMAP[label]] = CICOUNTS.get(FJLABELMAP[label], 0) + 1
                            LAUNCHEDJOBS.add(job['id'])

            if not CICOUNTS:
                print("No jobs to launch.")
                return

            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {GHTOKEN}"
            }
            payload = {
                "ref": "master",
                "inputs": CICOUNTS
            }
            async with session.post(f"{GHAPIURL}/repos/{GHREPO}/actions/workflows/{GHCIID}/dispatches", headers=headers, json=payload) as response:
                if response.status != 204:
                    print(f"Failed to trigger GitHub CI: {response.status}")
                    return
                print(f"GitHub CI triggered: {CICOUNTS} for {LAUNCHEDJOBS}")

        for job_id in LAUNCHEDJOBS:
            self.handled_jobs_cache[job_id] = True


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
