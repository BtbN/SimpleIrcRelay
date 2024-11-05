import requests

from trac.core import *
from trac.ticket.api import ITicketChangeListener
from trac.config import Option
from trac.util.text import exception_to_unicode


class SimpleIrcRelay(Component):
    implements(ITicketChangeListener)

    message_url = Option("notification", "irc_relay_url", "http://localhost:8787/message")

    def ticket_created(self, ticket):
        if self.message_url is None:
            return
        base_url = self._base_url()
        msg = f"[newticket] {ticket['reporter']}: Ticket #{ticket.id} ([{ticket['component']}] {ticket['summary']}) created {base_url}ticket/{ticket.id}"
        self._send_msg(msg)

    def ticket_changed(self, ticket, comment, author, old_values):
        if self.message_url is None or not comment:
            return
        base_url = self._base_url()
        msg = f"[editedticket] {author or 'unknown'}: Ticket #{ticket.id} ([{ticket['component']}] {ticket['summary']}) updated {base_url}ticket/{ticket.id}"
        if hasattr(ticket, 'cur_cnum'):
            msg += f"#comment:{ticket.cur_cnum}"
        self._send_msg(msg)

    def ticket_deleted(self, ticket):
        pass

    def ticket_comment_modified(self, ticket, cdate, author, comment, old_comment):
        pass

    def ticket_change_deleted(self, ticket, cdate, changes):
        pass

    def _base_url(self):
        base_url = self.config.get("trac", "base_url", "https://unset/")
        if not base_url.endswith("/"):
            base_url += "/"
        return base_url

    def _send_msg(self, msg):
        try:
            req = requests.put(self.message_url, data=msg)
            req.raise_for_status()
        except Exception as e:
            self.log.error("Failed putting to IRC relay: %s", exception_to_unicode(e))
