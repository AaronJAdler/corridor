"""The address rule, checked against the simulator's independent implementation of it."""

import string

from hypothesis import given
from hypothesis import strategies as st

from corridor.providers import is_valid_address
from corridor_sim import custody as sim_custody
from corridor_sim.ids import IdFactory

BASE32 = string.ascii_lowercase + "234567"
bodies = st.text(alphabet=BASE32, min_size=32, max_size=32)
# Wider than either alphabet, so a change can leave the alphabet as well as stay inside it.
characters = st.sampled_from(string.ascii_letters + string.digits + " -_.é١")


def test_the_contracts_example_address_is_valid() -> None:
    assert is_valid_address("sim1corridorexampledepositaddress234f24b41da")


@given(body=bodies)
def test_an_address_the_simulator_builds_is_valid(body: str) -> None:
    assert is_valid_address(sim_custody.address_for(body))


@given(seed=st.integers(0, 2**32))
def test_an_address_the_simulator_generates_is_valid(seed: int) -> None:
    body = IdFactory(seed).base32(32)
    assert is_valid_address(sim_custody.address_for(body))


@given(body=bodies, position=st.integers(0, 43), replacement=characters)
def test_any_one_character_change_makes_an_address_invalid(
    body: str, position: int, replacement: str
) -> None:
    address = sim_custody.address_for(body)
    changed = address[:position] + replacement + address[position + 1 :]
    if changed != address:
        assert not is_valid_address(changed)


@given(body=bodies, position=st.integers(0, 44), extra=characters)
def test_an_address_with_a_character_added_or_removed_is_invalid(
    body: str, position: int, extra: str
) -> None:
    address = sim_custody.address_for(body)
    assert not is_valid_address(address[:position] + extra + address[position:])
    removed = position % len(address)
    assert not is_valid_address(address[:removed] + address[removed + 1 :])


@given(text=st.one_of(st.text(max_size=60), st.from_regex(r"sim1[a-z2-7]{32}[0-9a-f]{8}")))
def test_both_sides_judge_any_text_alike(text: str) -> None:
    assert is_valid_address(text) == sim_custody.is_valid_address(text)


def test_a_trailing_newline_is_not_part_of_an_address() -> None:
    address = sim_custody.address_for("a" * 32)
    assert not is_valid_address(address + "\n")


def test_an_upper_case_address_is_invalid() -> None:
    address = sim_custody.address_for("a" * 32)
    assert not is_valid_address(address.upper())
    assert not is_valid_address(address[:4] + address[4:].upper())
