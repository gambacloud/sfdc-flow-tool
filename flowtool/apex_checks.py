"""
Code-quality checks on Apex source that need no compiler.

The prompt tells the model the rules ("no SOQL or DML inside a loop", "never a
hard-coded record Id", ...) but a prompt is a request, not a guarantee. What can
be checked in code is checked here, and a finding goes back to the model as a
repair, before the org ever sees the class. Each check is deliberately
conservative: a false alarm costs a repair round, a missed one only costs what
it always did, so when the text is ambiguous these checks stay quiet.

Everything works on a "masked" copy of the source - comments blanked, the inside
of string literals blanked - with the same length and line breaks as the
original, so a match position is also a line number in what the model wrote.
"""

from __future__ import annotations

import re
from typing import List, Tuple

_SOQL_RE = re.compile(r"\[\s*select\b", re.IGNORECASE)
_DYNAMIC_QUERY_RE = re.compile(r"\bDatabase\s*\.\s*(?:query|countQuery|getQueryLocator)\s*\(", re.IGNORECASE)
_DML_STATEMENT_RE = re.compile(
    r"(?:^|[;{}])\s*(insert|update|delete|upsert|undelete)\s+[A-Za-z_\[(]", re.IGNORECASE
)
_DML_CALL_RE = re.compile(
    r"\bDatabase\s*\.\s*(insert|update|delete|upsert|undelete)(?:Immediate)?\s*\(", re.IGNORECASE
)
_LOOP_RE = re.compile(r"\b(for|while)\s*\(")
_ASSERT_RE = re.compile(r"\bSystem\s*\.\s*assert\w*\s*\(|\bAssert\s*\.\s*\w+\s*\(", re.IGNORECASE)
_SEE_ALL_DATA_RE = re.compile(r"seeAllData\s*=\s*true", re.IGNORECASE)
_IS_TEST_RE = re.compile(r"@\s*isTest\b", re.IGNORECASE)
# Salesforce record Ids start with a 3-character object key prefix. Only well known
# prefixes are matched so an ordinary 15 or 18 character string is left alone.
_RECORD_ID_RE = re.compile(
    r"'(?:001|003|005|006|00Q|00D|00e|012|015|500|701|0Hn|0HM|a0[0-9A-Za-z])[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?'"
)
_CLASS_START_RE = re.compile(r"\bclass\s+\w+")
# Words Apex reserves that a model plausibly uses as a variable name (`List<Account> bulk = ...`
# fails to compile: 'Unexpected token'). Only declarations are matched, so the same words in
# SOQL or as part of a longer name are left alone.
_RESERVED = (
    "bulk activate array begin byte cast char collect const end exit export goto hint import "
    "inner into join like loop outer parallel retrieve returning search short sort stat then "
    "transaction when"
).split()
_RESERVED_DECL_RE = re.compile(
    r"\b\w+(?:<[^;(){}]*>)?(?:\[\])?\s+(" + "|".join(_RESERVED) + r")\s*(?:=|;|,|\)|:)"
)


def _mask(source: str) -> Tuple[str, str]:
    """(without_comments, masked): same length as `source`, newlines kept.
    `without_comments` keeps string literals; `masked` also blanks their insides."""
    plain: List[str] = []
    masked: List[str] = []
    i, n = 0, len(source)
    while i < n:
        two = source[i:i + 2]
        ch = source[i]
        if two == "//":
            j = source.find("\n", i)
            j = n if j == -1 else j
            blank = " " * (j - i)
            plain.append(blank)
            masked.append(blank)
            i = j
        elif two == "/*":
            j = source.find("*/", i + 2)
            j = n if j == -1 else j + 2
            blank = "".join(c if c == "\n" else " " for c in source[i:j])
            plain.append(blank)
            masked.append(blank)
            i = j
        elif ch == "'":
            j = i + 1
            while j < n and source[j] != "'":
                j += 2 if source[j] == "\\" else 1
            j = min(j + 1, n)
            literal = source[i:j]
            plain.append(literal)
            masked.append(literal[0] + "".join(c if c == "\n" else " " for c in literal[1:-1]) + literal[-1:]
                          if len(literal) > 1 else literal)
            i = j
        else:
            plain.append(ch)
            masked.append(ch)
            i += 1
    return "".join(plain), "".join(masked)


def _matching(text: str, start: int, opener: str, closer: str) -> int:
    """Index of the closer that matches the opener at `start`, or -1."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _loop_bodies(masked: str) -> List[Tuple[int, int]]:
    """(start, end) of the statement(s) each for/while loop repeats. A loop's own
    header is not part of its body - `for (A a : [SELECT ...])` queries once."""
    bodies: List[Tuple[int, int]] = []
    for match in _LOOP_RE.finditer(masked):
        close = _matching(masked, match.end() - 1, "(", ")")
        if close == -1:
            continue
        j = close + 1
        while j < len(masked) and masked[j].isspace():
            j += 1
        if j >= len(masked):
            continue
        if masked[j] == "{":
            end = _matching(masked, j, "{", "}")
            if end != -1:
                bodies.append((j, end))
        else:
            end = masked.find(";", j)
            if end != -1:
                bodies.append((j, end))
    return bodies


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _in_any(index: int, spans: List[Tuple[int, int]]) -> bool:
    return any(start <= index <= end for start, end in spans)


def loop_errors(masked: str) -> List[str]:
    """A query or DML statement inside a loop runs once per iteration and hits the
    governor limits on a bulk load."""
    spans = _loop_bodies(masked)
    if not spans:
        return []
    problems: List[str] = []
    for regex, what in ((_SOQL_RE, "a SOQL query"), (_DYNAMIC_QUERY_RE, "a dynamic query")):
        for m in regex.finditer(masked):
            if _in_any(m.start(), spans):
                problems.append(
                    f"line {_line(masked, m.start())}: {what} inside a loop - query once before the loop "
                    "(collect the Ids first, then `WHERE Id IN :ids`) and look results up in a Map"
                )
                break
    for regex in (_DML_STATEMENT_RE, _DML_CALL_RE):
        for m in regex.finditer(masked):
            if _in_any(m.start(1) if regex is _DML_STATEMENT_RE else m.start(), spans):
                problems.append(
                    f"line {_line(masked, m.start())}: DML (`{m.group(1)}`) inside a loop - add the records to a "
                    "List inside the loop and run the DML once after it"
                )
                break
    return problems


def reserved_word_errors(masked: str) -> List[str]:
    m = _RESERVED_DECL_RE.search(masked)
    if not m:
        return []
    word = m.group(1)
    return [
        f"line {_line(masked, m.start(1))}: `{word}` is a reserved word in Apex and cannot be a variable name "
        f"- rename it (for example `{word}Records` or `my{word.capitalize()}`)"
    ]


def hardcoded_id_errors(plain: str) -> List[str]:
    m = _RECORD_ID_RE.search(plain)
    if not m:
        return []
    return [
        f"line {_line(plain, m.start())}: a hard-coded record Id ({m.group(0)}) - an Id differs from org to org. "
        "Look the record up (by DeveloperName, Name or a custom setting) instead"
    ]


def sharing_errors(masked: str) -> List[str]:
    """A top-level class that states no sharing mode runs without sharing by default."""
    m = _CLASS_START_RE.search(masked)
    if not m:
        return []
    prefix = masked[:m.start()]
    if re.search(r"\b(with|without|inherited)\s+sharing\b", prefix, re.IGNORECASE):
        return []
    return [
        "the top-level class states no sharing mode - declare it `with sharing` (or `inherited sharing`; "
        "`without sharing` only when the request needs to bypass the user's access, and then say why in a comment)"
    ]


def unit_test_errors(masked: str) -> List[str]:
    problems: List[str] = []
    if _SEE_ALL_DATA_RE.search(masked):
        problems.append("`SeeAllData=true` makes a test depend on the org's data - create the records the test needs")
    if not _ASSERT_RE.search(masked):
        problems.append(
            "the test class never asserts anything - a test that cannot fail proves nothing. "
            "Check the outcome with `System.assertEquals(expected, actual, 'message')` or `Assert.areEqual`"
        )
    return problems


def quality_errors(body: str, *, check_sharing: bool = True) -> List[str]:
    """Every check that applies to this source (a class, or a trigger with
    `check_sharing=False`, since a trigger has no sharing mode)."""
    plain, masked = _mask(body)
    problems = loop_errors(masked) + reserved_word_errors(masked)
    if _IS_TEST_RE.search(masked):
        problems += unit_test_errors(masked)
        return problems
    problems += hardcoded_id_errors(plain)
    if check_sharing:
        problems += sharing_errors(masked)
    return problems
