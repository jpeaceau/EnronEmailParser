import re
from typing import Set, Optional, Tuple
from datetime import datetime, timezone
import src.global_utils as global_utils
from dateutil.parser import parse

TIMEZONE_MAP = {
    'PST': 'America/Los_Angeles', 'PDT': 'America/Los_Angeles',
    'MST': 'America/Denver',    'MDT': 'America/Denver',
    'CST': 'America/Chicago',   'CDT': 'America/Chicago',
    'EST': 'America/New_York',    'EDT': 'America/New_York',
}

def _clean_date_string(date_string: str) -> str:
    """
    Replaces ambiguous timezone abbreviations in a date string with their
    unambiguous IANA counterparts before parsing.
    """
    # The regex looks for a timezone abbreviation in parentheses at the end of the string
    # e.g., "Mon, 2 Oct 2000 10:30:00 -0700 (PDT)" -> captures "PDT"
    match = re.search(r'\((\w{3})\)$', date_string)
    if match:
        tz_abbr = match.group(1)
        if tz_abbr in TIMEZONE_MAP:
            # Replace the abbreviation with an empty string, as the UTC offset is sufficient
            # and the IANA name is not needed by the parser if an offset is present.
            # This avoids parsing conflicts.
            return date_string.replace(f'({tz_abbr})', '').strip()
    return date_string

def _extract_users(text, regex) -> Set[str]:
    """

    :param text: The relevant section of text.
    :param regex: The appropriate regular expression for identifying and separating aliases.
    :return: Aliases converted into a set.
    """
    users = set()
    users_re = re.match(regex, text)
    if not global_utils.is_regex_populated(users_re, "User extraction", text, False, True):
        return users
    for user in users_re.groups():
        users.add(user)
    return users

def _parse_parent_date(date_string) -> tuple[datetime, datetime] | None:
    """
    Parses a date string and returns a tuple of
    (original_datetime, normalized_datetime_naive).
    :param date_string: The relevant text that contains the parent date data.
    :raises ValueError
    :return A copy of the original datetime, and a copy of the datetime normalized to 12:00 UTC.
    """
    try:
        cleaned_date_string = _clean_date_string(date_string)
        original_dt_aware = parse(cleaned_date_string).astimezone(timezone.utc)
        normalized_dt_utc = original_dt_aware.astimezone(timezone.utc)
        return original_dt_aware, normalized_dt_utc

    except ValueError:
        # Re-raise the error with context if parsing fails.
        raise ValueError(f"Invalid parent email date format. Context:\n{date_string}")

def _parse_child_email_date(date_string: str, parent_timezone: timezone) -> Optional[
    Tuple[datetime, datetime]]:
    """
    Parses a child email date, applies the parent's timezone, and returns a tuple of
    (original_datetime_aware, normalized_datetime_naive).
    :param date_string: The relevant text that includes the child email date's data.
    :param parent_timezone: The child date's timezone presumes to be the parent email's timezone.
    :raises ValueError
    :return A copy of the original datetime, and a copy of the datetime normalized to 12:00 UTC.
    """
    try:
        cleaned_date_string = _clean_date_string(date_string)
        dt_naive = parse(cleaned_date_string)
        original_dt_aware = dt_naive.replace(tzinfo=parent_timezone)
        normalized_dt_utc = original_dt_aware.astimezone(timezone.utc)
        return original_dt_aware, normalized_dt_utc
    except ValueError:
        raise ValueError(f"Invalid child email date format. Context:\n{date_string}")


def _extract_parent_users(email, fields: list[list[str]], regex) -> Tuple[Set[str], str]:
    """
    This function is specifically designed for the parent fields of To: From: Cc: And the same with the "X-" prefix.
    :param email: The email in text form.
    :param fields: The boundaries of where to extract text between.
    :param regex: The regular expression to apply on the result of the text retrieved between the specified boundaries.
    :return: All aliases and the sender alias.
    """
    aliases: Set[str] = set()
    sender = ""
    for to, from_ in fields:
        users_text = _extract_between_fields(email, to, from_)
        if not users_text:
            continue

        users_re = re.search(regex, users_text)
        if to == "From" or to == "X-From":
            sender = users_re.group(1) if users_re and users_re.groups() else users_text
            aliases.add(sender)
            continue
        # Expected match is False, as To and Cc aren't always present.
        if not global_utils.is_regex_populated(users_re, "Extracting parent users", email, False):
            continue
        aliases.update(users_re.groups())
    return aliases, sender

def _extract_between_fields(email: str, start_field: str, end_field: str, multi_line: bool = True) -> Optional[str]:
    """
    Extracts content between a start and end header field.
    This is particularly useful for extracting the message body.
    """
    flags = re.DOTALL if multi_line else 0
    regex_pattern = f"(?:{start_field}:\\s*)(.*?)(?:\n{end_field}:)"
    match = re.search(regex_pattern, email, flags=flags)
    if not global_utils.is_regex_populated(match, f"Extraction between {start_field} and {end_field}", email, False, False):
        return None
    return match.group(1).strip()
