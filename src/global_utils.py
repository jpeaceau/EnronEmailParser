def is_regex_populated(regex_obj, description, context: str, expected_match=True, expected_groups=True) -> bool:
    """
    Due to consistent regex-related checks needed, this function allows specification on if a match is required and if
    a match is found, if a group is required.
    """
    if not regex_obj:
        if expected_match:
            raise ValueError(f"Regex could not find a match. Description: {description}\n"
                             f"Context: \n{context}")
        else:
            return False
    elif not regex_obj.groups():
        if expected_groups:
            raise ValueError(f"Regex match found, but no groups were found. Description: {description}\n"
                             f"Context: \n{context}")
        else:
            return False
    return True
