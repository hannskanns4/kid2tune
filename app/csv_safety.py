"""Helpers for creating CSV files safe to open in spreadsheet software."""


def spreadsheet_safe_cell(value) -> str:
    value = str(value or "")
    probe = value.lstrip(" \t\r\n")
    if probe.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
