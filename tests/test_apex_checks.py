"""Code-quality checks on Apex that need no compiler: each fires on the mistake and stays quiet on the look-alike."""

import pytest

from flowtool.apex_checks import quality_errors
from flowtool.ir_apex import heuristic_errors, heuristic_trigger_errors


def cls(body, header="public with sharing class A"):
    return f"{header} {{\n{body}\n}}"


def test_a_query_inside_a_loop_is_found_with_its_line():
    body = cls("  void run(List<Id> ids) {\n    for (Id i : ids) {\n      Account a = [SELECT Id FROM Account WHERE Id = :i];\n    }\n  }")
    problems = quality_errors(body)
    assert len(problems) == 1 and "SOQL query inside a loop" in problems[0] and "line 4" in problems[0]


def test_the_loop_header_query_is_not_a_query_per_iteration():
    body = cls("  void run() {\n    for (Account a : [SELECT Id FROM Account]) {\n      a.Name = 'x';\n    }\n  }")
    assert quality_errors(body) == []


def test_dml_in_a_loop_is_found_in_both_spellings():
    statement = cls("  void run(List<Account> l) {\n    for (Account a : l) {\n      update a;\n    }\n  }")
    call = cls("  void run(List<Account> l) {\n    while (true) {\n      Database.insert(l);\n    }\n  }")
    assert any("DML (`update`) inside a loop" in p for p in quality_errors(statement))
    assert any("DML (`insert`) inside a loop" in p for p in quality_errors(call))


def test_dml_after_the_loop_and_words_in_strings_and_comments_are_fine():
    body = cls("  void run(List<Account> l) {\n    List<Account> out = new List<Account>();\n"
               "    for (Account a : l) {\n      // update a;\n      String s = 'insert [SELECT x]';\n      out.add(a);\n    }\n    update out;\n  }")
    assert quality_errors(body) == []


def test_a_one_statement_loop_without_braces_is_checked_too():
    body = cls("  void run(List<Id> ids) {\n    for (Id i : ids) delete [SELECT Id FROM Account WHERE Id = :i];\n  }")
    assert quality_errors(body)


def test_a_hardcoded_record_id_is_found_but_an_ordinary_string_is_not():
    bad = cls("  Id owner = '005000000000001AAA';")
    good = cls("  String s = 'abcdefghijklmno'; String t = 'not an id at all';")
    assert any("hard-coded record Id" in p for p in quality_errors(bad))
    assert quality_errors(good) == []


@pytest.mark.parametrize("header, ok", [
    ("public with sharing class A", True),
    ("public inherited sharing class A", True),
    ("public without sharing class A", True),
    ("public class A", False),
    ("global class A", False),
])
def test_a_class_must_state_its_sharing_mode(header, ok):
    assert (quality_errors(cls("", header)) == []) is ok


def test_a_trigger_has_no_sharing_mode_to_state():
    assert heuristic_trigger_errors("T", "trigger T on Account (before insert) {\n  for (Account a : Trigger.new) { a.Name = 'x'; }\n}") == []


def test_a_trigger_with_a_query_in_its_loop_is_rejected():
    body = "trigger T on Contact (before insert) {\n  for (Contact c : Trigger.new) {\n    Account a = [SELECT Id FROM Account WHERE Id = :c.AccountId];\n  }\n}"
    assert any("SOQL query inside a loop" in p for p in heuristic_trigger_errors("T", body))


def test_a_test_class_must_assert_and_must_not_see_all_data():
    no_assert = "@isTest\npublic class ATest {\n  @isTest static void t() { Account a = new Account(Name='x'); insert a; }\n}"
    see_all = "@isTest(SeeAllData=true)\npublic class ATest {\n  @isTest static void t() { System.assertEquals(1, 1); }\n}"
    good = "@isTest\npublic class ATest {\n  @isTest static void t() { System.assertEquals(1, 1, 'one'); }\n}"
    assert any("never asserts" in p for p in quality_errors(no_assert))
    assert any("SeeAllData" in p for p in quality_errors(see_all))
    assert quality_errors(good) == []


def test_a_test_class_needs_no_sharing_mode_and_may_use_a_made_up_id():
    body = "@isTest\npublic class ATest {\n  @isTest static void t() { Id x = '001000000000001AAA'; System.assert(x != null); }\n}"
    assert quality_errors(body) == []


def test_the_class_heuristics_report_these_findings_to_the_repair_loop():
    body = cls("  void run(List<Id> ids) {\n    for (Id i : ids) { Account a = [SELECT Id FROM Account WHERE Id = :i]; }\n  }")
    assert any("inside a loop" in p for p in heuristic_errors("A", body))


def test_a_reserved_word_as_a_variable_name_is_found_with_a_fix():
    body = cls("  void run() {\n    List<Account> bulk = new List<Account>();\n  }")
    problems = quality_errors(body)
    assert len(problems) == 1 and "`bulk` is a reserved word" in problems[0] and "line 3" in problems[0]


def test_reserved_words_inside_longer_names_soql_and_strings_are_fine():
    body = cls("  void run() {\n    List<Account> bulkAccounts = [SELECT Id FROM Account GROUP BY Id];\n"
               "    String s = 'bulk end sort';\n    Integer endCount = 0; // bulk end\n  }")
    assert quality_errors(body) == []


def test_a_reserved_word_as_a_loop_variable_is_found():
    body = cls("  void run(List<Account> l) {\n    for (Account end : l) { System.debug(end); }\n  }")
    assert any("`end` is a reserved word" in p for p in quality_errors(body))
