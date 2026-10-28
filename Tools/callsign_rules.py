"""Conservative slash-call parsing shared by the importer and checker."""

import re


OPERATING_SUFFIXES = {"P", "M", "MM", "AM", "QRP", "QRPP"}


def base_call_span(call, *, allow_partial=False, allow_wildcards=False):
    """Return one base span; GUI callers may allow a digit-ending partial base."""
    if allow_wildcards and "?" in call:
        parts = call.split("/")
        wildcard_parts = [index for index, part in enumerate(parts) if "?" in part]
        if len(wildcard_parts) != 1:
            return None
        spans = set()
        for replacement in ("A", "0"):
            probe = call.replace("?", replacement)
            span = base_call_span(probe, allow_partial=allow_partial)
            if span is not None:
                start = sum(len(part) + 1 for part in parts[:wildcard_parts[0]])
                if span == (start, start + len(parts[wildcard_parts[0]])):
                    spans.add(span)
        return spans.pop() if len(spans) == 1 else None
    if not re.fullmatch(r"[A-Z0-9]+(?:/[A-Z0-9]+)*", call):
        return None
    parts = call.split("/")
    candidates = [
        index for index, part in enumerate(parts)
        if re.fullmatch(r"[A-Z0-9]*[A-Z][A-Z0-9]*[0-9][A-Z]+", part)
    ]
    if not candidates and allow_partial:
        candidates = [
            index for index, part in enumerate(parts)
            if re.fullmatch(r"[A-Z0-9]*[A-Z][A-Z0-9]*[0-9]", part)
        ]
    if len(candidates) != 1:
        return None
    selected = candidates[0]
    for index, part in enumerate(parts):
        if index == selected:
            continue
        is_prefix = (
            bool(re.fullmatch(r"[A-Z]{1,3}", part))
            or (len(part) <= 4 and part[-1].isdigit()
                and any(char.isalpha() for char in part))
        )
        if index < selected:
            if index != 0 or selected != 1 or not is_prefix:
                return None
        elif not (is_prefix or part in OPERATING_SUFFIXES
                  or re.fullmatch(r"[0-9]", part)):
            return None
    start = sum(len(part) + 1 for part in parts[:selected])
    return start, start + len(parts[selected])
