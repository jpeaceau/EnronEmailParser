import hashlib
from typing import Dict, Set, Optional, FrozenSet
from datetime import timezone
import re
from . import _helpers as helpers
import src.global_utils as global_utils

from src.data_object.processed_email import ProcessedEmail

class EmailPipeline:

    def __init__(self):
        self.parent_msg_filter = r"X-bcc:.*?\n(.*)"
        self.child_msg_filter = r"Subject:.*?\n(.*)"
        self.child_date_filter = r"(?:Sent|Date):\s*(.*)\n"
        self.subject_filter = r"Subject:\s*(.*)\n"
        # Used for X-From: X-To: X-cc
        self.x_filter = r"(?:\s*,\s*)?(\w+,\s+.*?(?=\s*<|\.\s*)|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6})"
        # Used for From: To: and CC:, parent email only.
        self.non_x_filter = r"\s*([^,]+@[^,]+)(?=\s*,|$)"
        # Used for child From: To: and CC: fields.
        self.child_user_filter = r"(?:\s*,\s*)?(\w+,\s+.*?(?=\s*\(|\.|\[|;|'\s*))"

    def process_file_contents(self, canon_email: str, message_cache: Dict[str, timezone], lock) -> Optional[Set[ProcessedEmail]]:
        """
        Processes a file's parent and child emails. Additional parameters are required, due to parallel processing.
        :param canon_email: The email text in canonical format.
        :param message_cache: The message cache instance.
        :param lock: The lock object from the worker pool manager to prevent race-like conditions.
        :return: A set of processed emails. If all are already cached, None is returned.
        """
        email_split = canon_email.split("-----Original Message-----")
        processed_emails: Set[ProcessedEmail] = set()
        email_split_size = len(email_split)

        parent_email = canon_email if email_split_size <= 1 else email_split[0]
        child_emails: set[str] = set() if email_split_size <= 1 else email_split[1:-1]

        parent_processed_email, parent_timezone = self._process_parent_email(parent_email, message_cache, lock)
        parent_hash = ""
        if isinstance(parent_processed_email, ProcessedEmail):
            processed_emails.add(parent_processed_email)
            parent_hash = parent_processed_email.email_hash
        elif isinstance(parent_processed_email, str):
            # Parent was cached, parent_processed_email is the hash string
            parent_hash = parent_processed_email
        for child_email in child_emails:
            processed_child_email = self._process_child_email(child_email, parent_timezone, message_cache, lock, parent_hash)
            if processed_child_email:
                processed_emails.add(processed_child_email)
        return processed_emails


    def _process_parent_email(self, email: str, message_cache, lock) -> tuple[ProcessedEmail, timezone] | tuple[str, timezone]:
        """
        The orchestra for managing the parent email only.
        :param email: Parent email, absent of any "---- Original Message ----" objects and text thereafter.
        :param message_cache: The instance of the message cache.
        :param lock: Pool manager lock.
        :return: ProcessedEmail object and the timezone extracted from the date data. If cached, the hash is returned instead.
        """
        msg_re = re.search(self.parent_msg_filter, email, flags=re.DOTALL)
        global_utils.is_regex_populated(msg_re, "Parent email filter", email)
        msg = msg_re.group(1)
        msg_hash = hashlib.md5()
        msg_hash.update(msg.encode("utf-8"))
        msg_hash = msg_hash.hexdigest()
        with lock:
            if msg_hash in message_cache:
                return msg_hash, message_cache[msg_hash]
        # Date prefix is different for parent and child emails.
        date_re = re.search(r"Date:\s*(.*)\n", email)
        global_utils.is_regex_populated(date_re, "Parent date field", email)
        subject_text = helpers._extract_between_fields(email, start_field="Subject", end_field="Mime-Version")

        date_obj, norm_date_obj = helpers._parse_parent_date(date_re.group(1))

        boundaries = [["From", "To"], ["To", "Subject"], ["Cc", "Mime-Version"]]
        aliases, sender = helpers._extract_parent_users(email, boundaries, self.non_x_filter)

        x_boundaries = [["X-From", "X-To"], ["X-To", "X-cc"], ["X-cc", "X-bcc"]]
        x_aliases, x_sender = helpers._extract_parent_users(email, x_boundaries, self.x_filter)
        senders = [sender, x_sender] if sender else [x_sender] # "From:" is not always present
        senders = frozenset(senders)

        processed_email = ProcessedEmail(
            email_hash=msg_hash,
            date=date_obj,
            norm_date=norm_date_obj,
            subject=subject_text,
            aliases=frozenset({alias for alias in aliases if alias}),
            sender=senders,
            parent_hash=""
        )
        with lock:
            message_cache[msg_hash] = date_obj.tzinfo
        return processed_email, date_obj.tzinfo


    def _process_child_email(self, email: str, parent_timezone: timezone, message_cache, lock, parent_hash: str) -> ProcessedEmail | None:
        """
        The orchestra for child emails only.
        :param email: Text representing a child email.
        :param parent_timezone: Parent email timezone; it's assumed this and the child email's timezone are consistent.
        :param message_cache: The message cache instance for querying and updating.
        :param lock: Lock to access and update message_cache without race-conditions.
        :param parent_hash: The hash of the parent email to link thread relationships.
        :return: ProcessedEmail objects, or None if all are already cached.
        """
        msg_re = re.search(self.child_msg_filter, email, flags=re.DOTALL)
        date_re = re.search(self.child_date_filter, email)
        subject_re = re.search(self.subject_filter, email, flags = re.DOTALL)
        global_utils.is_regex_populated(date_re, "Child date filter", email)
        # The > character may be used legitimately, but the purpose of this data is not NLP, but quantitative insights. As such,
        # it's more efficient to strip > entirely when used as a prefix of new lines in child messages.
        if not msg_re or not msg_re.groups():
            return None
        msg = msg_re.group(1).strip(">")
        msg_hash = hashlib.md5()
        msg_hash.update(msg.encode("utf-8"))
        msg_hash = msg_hash.hexdigest()
        with lock:
            if msg_hash in message_cache:
                return None
            message_cache[msg_hash] = parent_timezone
        date, norm_date = helpers._parse_child_email_date(date_string=date_re.group(1), parent_timezone=parent_timezone)
        subject = None if not (subject_re or subject_re.groups()) else subject_re.group(1)
        from_text = re.search(r"From:\s+(.*)$", email)
        to_text = helpers._extract_between_fields(email, "To", "Subject")
        users = set()
        from_alias = ""
        if from_text:
            from_alias = helpers._extract_users(
                text=from_text.group(0),
                regex=self.child_user_filter
            )
            if not from_alias:
                from_alias = {from_text.group(0)}
            users.update(from_alias)
        if to_text:
            users.update(helpers._extract_users(
                text=to_text,
                regex=self.child_user_filter
            ))
        return ProcessedEmail(
            email_hash=msg_hash,
            date=date,
            norm_date=norm_date,
            subject=subject,
            aliases=frozenset({user for user in users if user}),
            sender=frozenset(from_alias) if from_alias else frozenset(),
            parent_hash=parent_hash if parent_hash else None
        )
