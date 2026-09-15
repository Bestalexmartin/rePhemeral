"""Masked password entry, driven by a scripted keyboard.

The terminal half of `setup`'s prompt needs a real console, which pytest
does not have, so the key handling is tested apart from it.
"""
import pytest

from rephemeral.cli import _masked_input


def _type(keys):
    """Feed keystrokes to the prompt. Returns (value, everything echoed)."""
    pending = iter(keys)
    shown = []
    value = _masked_input('pw: ', lambda: next(pending), shown.append)
    return value, ''.join(shown)


def test_each_character_echoes_one_asterisk_and_never_itself():
    value, shown = _type([*'s3cret', '\r'])
    assert value == 's3cret'
    assert shown == 'pw: ******\n'


def test_enter_as_newline_also_submits():
    assert _type(['a', '\n'])[0] == 'a'


@pytest.mark.parametrize('backspace', ['\x08', '\x7f'])
def test_backspace_removes_a_character_and_its_asterisk(backspace):
    value, shown = _type(['a', 'b', backspace, 'c', '\r'])
    assert value == 'ac'
    assert shown == 'pw: **\b \b*\n'


def test_backspace_on_empty_input_erases_nothing():
    value, shown = _type(['\x08', 'x', '\r'])
    assert value == 'x'
    assert shown == 'pw: *\n'


def test_keys_that_are_not_characters_are_ignored():
    value, shown = _type([None, 'a', '\x1b', '\t', 'b', '\r'])
    assert value == 'ab'
    assert shown == 'pw: **\n'


def test_ctrl_c_aborts():
    with pytest.raises(KeyboardInterrupt):
        _type(['a', '\x03'])


@pytest.mark.parametrize('end', ['', '\x04'])
def test_end_of_input_aborts(end):
    with pytest.raises(EOFError):
        _type(['a', end])
