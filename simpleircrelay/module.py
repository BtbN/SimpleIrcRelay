import requests

from trac.core import *
from trac.ticket.api import ITicketChangeListener
from trac.config import Option


class SimpleIrcRelay(Component):
    implements(ITicketChangeListener)

    message_url = Option("notification", "irc_relay_url", "http://localhost:8787/message")

    def ticket_created(self, ticket):
        if self.message_url is None:
            return
        base_url = self.config.get('trac', 'base_url', 'https://unset/')
        msg = f"[newticket] {ticket['reporter']}: Ticket #{ticket.id} ([{ticket['component']}] {ticket['summary']}) created {base_url}ticket/{ticket.id}"
        self._send_msg(msg)

    def ticket_changed(self, ticket, comment, author, old_values):
        pass

    def ticket_deleted(self, ticket):
        pass

    def ticket_comment_modified(self, ticket, cdate, author, comment, old_comment):
        pass

    def ticket_change_deleted(self, ticket, cdate, changes):
        pass

    def _send_msg(self, msg):
        req = requests.put(self.message_url, data=msg)
        req.raise_for_status()
