"""The rate simulator: a seeded walk of mid-market rates, with pins and a freeze for tests."""

import itertools
import math
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from corridor_sim.clock import format_time
from corridor_sim.fx import RateWalk
from tests.simulators.conftest import API_KEY, START, Sim

Launch = Callable[..., Awaitable[Sim]]

ASSETS = ("USD", "MXN", "BRL", "USDC")
PAIRS = list(itertools.permutations(ASSETS, 2))
HALF_A_MILLIONTH = Decimal("0.0000005")


def code_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def after(seconds: float) -> str:
    return format_time(START + timedelta(seconds=seconds))


async def rate(sim: Sim, base: str, quote: str) -> dict[str, Any]:
    response = await sim.api.get(f"/fx/v1/rates/{base}/{quote}")
    assert response.status_code == 200, response.text
    document: dict[str, Any] = response.json()
    return document


async def mid(sim: Sim, base: str, quote: str) -> Decimal:
    return Decimal((await rate(sim, base, quote))["mid"])


async def all_mids(sim: Sim) -> dict[tuple[str, str], str]:
    return {pair: (await rate(sim, *pair))["mid"] for pair in PAIRS}


# --- authentication ------------------------------------------------------------------------


async def test_the_rate_route_refuses_a_request_without_the_api_key(sim: Sim) -> None:
    response = await sim.anonymous.get("/fx/v1/rates/USD/MXN")

    assert code_of(response) == (401, "unauthorized")


async def test_the_rate_route_refuses_a_wrong_api_key(sim: Sim) -> None:
    response = await sim.anonymous.get(
        "/fx/v1/rates/USD/MXN", headers={"Authorization": f"Bearer {API_KEY}-not"}
    )

    assert code_of(response) == (401, "unauthorized")


# --- pairs ---------------------------------------------------------------------------------


async def test_a_rate_is_the_mid_with_six_places_and_the_time_it_was_last_updated(
    sim: Sim,
) -> None:
    response = await sim.api.get("/fx/v1/rates/USD/MXN")

    assert response.status_code == 200
    assert response.json() == {
        "base": "USD",
        "quote": "MXN",
        "mid": "17.250000",
        "as_of": "2026-01-15T12:00:00Z",
    }


async def test_the_walk_starts_from_the_same_three_prices_every_time(sim: Sim) -> None:
    assert await all_mids(sim) == {
        ("USD", "MXN"): "17.250000",
        ("USD", "BRL"): "5.100000",
        ("USD", "USDC"): "1.000000",
        ("MXN", "USD"): "0.057971",
        ("MXN", "BRL"): "0.295652",
        ("MXN", "USDC"): "0.057971",
        ("BRL", "USD"): "0.196078",
        ("BRL", "MXN"): "3.382353",
        ("BRL", "USDC"): "0.196078",
        ("USDC", "USD"): "1.000000",
        ("USDC", "MXN"): "17.250000",
        ("USDC", "BRL"): "5.100000",
    }


@pytest.mark.parametrize("seconds", [0, 1, 7, 600])
async def test_every_ordered_pair_is_served_with_six_places_and_agrees_with_its_reverse(
    sim: Sim, seconds: int
) -> None:
    await sim.advance(seconds)

    mids = await all_mids(sim)

    assert set(mids) == set(PAIRS)
    assert len(PAIRS) == 12
    for (base, quote), text in mids.items():
        assert re.fullmatch(r"[0-9]+\.[0-9]{6}", text), (base, quote, text)
        forward, backward = Decimal(text), Decimal(mids[quote, base])
        assert forward > 0
        # Each is rounded to six places, so the product is 1 to within that rounding.
        assert abs(forward * backward - 1) <= HALF_A_MILLIONTH * (forward + backward) + Decimal(
            "1e-12"
        ), (base, quote)


async def test_every_rate_is_derived_from_the_same_prices(sim: Sim) -> None:
    await sim.advance(300)

    mxn_brl = await mid(sim, "MXN", "BRL")
    through_usd = (await mid(sim, "MXN", "USD")) * (await mid(sim, "USD", "BRL"))
    through_usdc = (await mid(sim, "MXN", "USDC")) * (await mid(sim, "USDC", "BRL"))

    # No arbitrage beyond what rounding each leg to six places allows.
    assert abs(mxn_brl - through_usd) < Decimal("0.00001")
    assert abs(mxn_brl - through_usdc) < Decimal("0.00001")


@pytest.mark.parametrize(
    "path",
    [
        "/fx/v1/rates/USD/EUR",
        "/fx/v1/rates/EUR/USD",
        "/fx/v1/rates/USD/USD",
        "/fx/v1/rates/USDC/USDC",
        "/fx/v1/rates/usd/mxn",
        "/fx/v1/rates/USD/DOGE",
    ],
)
async def test_an_unknown_pair_or_a_pair_of_one_asset_is_not_found(sim: Sim, path: str) -> None:
    response = await sim.api.get(path)

    assert code_of(response) == (404, "unknown_pair")


# --- the walk ------------------------------------------------------------------------------


async def test_rates_move_once_a_second_and_as_of_follows(sim: Sim) -> None:
    start = await rate(sim, "USD", "MXN")

    await sim.advance(0.999)
    within_the_second = await rate(sim, "USD", "MXN")
    await sim.advance(0.001)
    a_second_on = await rate(sim, "USD", "MXN")
    await sim.advance(59.5)
    a_minute_on = await rate(sim, "USD", "MXN")

    assert within_the_second == start
    assert a_second_on["as_of"] == after(1)
    assert a_second_on["mid"] != start["mid"]
    # The last step was at the last whole second, half a second ago.
    assert a_minute_on["as_of"] == after(60)
    assert a_minute_on["mid"] not in (start["mid"], a_second_on["mid"])


async def test_the_same_seed_and_the_same_time_give_identical_rates(launch: Launch) -> None:
    first, second = await launch(seed=42), await launch(seed=42)

    await first.advance(3600)
    await second.advance(3600)

    assert await all_mids(first) == await all_mids(second)
    assert (await rate(first, "USD", "MXN"))["as_of"] == after(3600)


async def test_a_different_seed_gives_different_rates(launch: Launch) -> None:
    first, second = await launch(seed=42), await launch(seed=43)

    await first.advance(60)
    await second.advance(60)

    one, other = await all_mids(first), await all_mids(second)
    assert one["USD", "MXN"] != other["USD", "MXN"]
    assert one["USD", "BRL"] != other["USD", "BRL"]
    assert one["USD", "USDC"] != other["USD", "USDC"]


async def test_the_rates_depend_on_the_time_elapsed_and_on_nothing_else(launch: Launch) -> None:
    in_one_step, in_many, busy = await launch(), await launch(), await launch()

    await in_one_step.advance(90)
    for seconds in (0.25, 0.75, 1, 28, 0, 59.999, 0.001):
        await in_many.advance(seconds)
    # Everything else the simulator does draws on other generators than the walk's.
    for _ in range(5):
        beneficiary = await busy.beneficiary()
        await busy.payout(beneficiary["id"])
        await busy.address(f"customer-{beneficiary['id']}")
        await rate(busy, "USD", "BRL")
        await busy.advance(18)

    assert await all_mids(in_many) == await all_mids(in_one_step)
    assert await all_mids(busy) == await all_mids(in_one_step)


async def test_the_walk_is_the_same_on_every_machine(sim: Sim) -> None:
    # The steps are made of integers drawn from a seeded generator, so these values do not
    # depend on a platform's floating point or its C library.
    await sim.advance(10)
    ten = await all_mids(sim)
    await sim.advance(990)
    thousand = await all_mids(sim)

    assert (ten["USD", "MXN"], ten["USD", "BRL"], ten["USD", "USDC"]) == GOLDEN[10]
    assert (thousand["USD", "MXN"], thousand["USD", "BRL"], thousand["USD", "USDC"]) == GOLDEN[1000]


# Seed 20260115, after 10 and after 1000 steps: USD/MXN, USD/BRL, USD/USDC.
GOLDEN = {
    10: ("17.240513", "5.101784", "1.000144"),
    1000: ("17.078032", "5.051807", "1.000111"),
}


def test_a_step_is_about_two_basis_points_and_half_of_one_for_usdc() -> None:
    walk = RateWalk(seed=20260115, start=START)
    series: dict[str, list[Decimal]] = {"MXN": [], "BRL": [], "USDC": []}
    for second in range(5001):
        walk.advance_to(START + timedelta(seconds=second))
        for asset, values in series.items():
            values.append(walk.rate("USD", asset).mid)

    def basis_points(values: list[Decimal]) -> tuple[float, float]:
        returns = [math.log(b / a) * 10_000 for a, b in itertools.pairwise(values)]
        mean = sum(returns) / len(returns)
        deviation = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
        return deviation, max(abs(r) for r in returns)

    mxn, brl, usdc = (basis_points(series[asset]) for asset in ("MXN", "BRL", "USDC"))
    # Over 5000 steps the measured deviation is within a few percent of the true one.
    assert 1.9 < mxn[0] < 2.1
    assert 1.9 < brl[0] < 2.1
    assert 0.47 < usdc[0] < 0.53
    # No step is a jump: the largest is a handful of deviations.
    assert 4 < mxn[1] < 12
    assert 4 < brl[1] < 12
    assert 1 < usdc[1] < 3


# --- freezing ------------------------------------------------------------------------------


async def test_freezing_stops_the_rates_and_as_of_while_time_goes_on(sim: Sim) -> None:
    await sim.advance(5)
    before = await all_mids(sim)

    frozen = await sim.control("POST", "/fx/freeze", {"frozen": True})
    await sim.advance(120)

    assert frozen == {"frozen": True, "as_of": after(5)}
    assert await all_mids(sim) == before
    assert (await rate(sim, "USD", "MXN"))["as_of"] == after(5)
    assert (await sim.control("GET", "/clock"))["now"] == after(125)


async def test_freezing_stops_only_the_rates(sim: Sim) -> None:
    beneficiary = await sim.beneficiary()
    payout = await sim.payout(beneficiary["id"])
    await sim.control("POST", "/fx/freeze", {"frozen": True})

    await sim.advance(30)

    assert (await sim.get_payout(payout["id"]))["status"] == "completed"


async def test_unfreezing_brings_the_rates_back_to_where_the_walk_is_by_now(
    launch: Launch,
) -> None:
    frozen, running = await launch(), await launch()
    await frozen.advance(5)
    await running.advance(5)
    await frozen.control("POST", "/fx/freeze", {"frozen": True})
    await frozen.advance(120)
    await running.advance(120)

    thawed = await frozen.control("POST", "/fx/freeze", {"frozen": False})

    assert thawed == {"frozen": False, "as_of": after(125)}
    assert await all_mids(frozen) == await all_mids(running)
    await frozen.advance(10)
    await running.advance(10)
    assert await all_mids(frozen) == await all_mids(running)


@pytest.mark.parametrize("body", [{}, {"frozen": "yes"}, {"frozen": 1}, {"frozen": None}])
async def test_a_malformed_freeze_is_refused(sim: Sim, body: object) -> None:
    response = await sim.anonymous.post("/_control/fx/freeze", json=body)

    assert code_of(response) == (422, "invalid_request")


# --- pinning -------------------------------------------------------------------------------


async def test_a_pinned_pair_returns_the_pinned_mid_and_its_reverse_the_reciprocal(
    sim: Sim,
) -> None:
    pinned = await sim.control("POST", "/fx/rates", {"base": "USD", "quote": "MXN", "mid": "18.5"})
    await sim.advance(300)

    assert pinned == {
        "base": "USD",
        "quote": "MXN",
        "mid": "18.500000",
        "as_of": "2026-01-15T12:00:00Z",
    }
    assert (await rate(sim, "USD", "MXN"))["mid"] == "18.500000"
    # 1 / 18.5 = 0.054054054...
    assert (await rate(sim, "MXN", "USD"))["mid"] == "0.054054"


async def test_a_pinned_rate_stays_fresh(sim: Sim) -> None:
    await sim.control("POST", "/fx/rates", {"base": "USD", "quote": "MXN", "mid": "18.500000"})

    await sim.advance(300)

    # Pinning fixes the value. Making a rate stale is what freezing is for.
    assert (await rate(sim, "USD", "MXN"))["as_of"] == after(300)
    assert (await rate(sim, "MXN", "USD"))["as_of"] == after(300)


async def test_pinning_one_pair_leaves_the_others_walking(launch: Launch) -> None:
    pinned, untouched = await launch(), await launch()
    await pinned.control("POST", "/fx/rates", {"base": "USD", "quote": "MXN", "mid": "18.500000"})

    await pinned.advance(60)
    await untouched.advance(60)

    there, here = await all_mids(untouched), await all_mids(pinned)
    assert {pair for pair in PAIRS if here[pair] != there[pair]} == {("USD", "MXN"), ("MXN", "USD")}


async def test_a_pair_can_be_pinned_again_and_from_either_side(sim: Sim) -> None:
    await sim.control("POST", "/fx/rates", {"base": "USD", "quote": "BRL", "mid": "5.000000"})

    await sim.control("POST", "/fx/rates", {"base": "BRL", "quote": "USD", "mid": "0.250000"})

    assert (await rate(sim, "BRL", "USD"))["mid"] == "0.250000"
    assert (await rate(sim, "USD", "BRL"))["mid"] == "4.000000"


@pytest.mark.parametrize(
    ("body", "refusal"),
    [
        ({"base": "USD", "quote": "EUR", "mid": "1.000000"}, (404, "unknown_pair")),
        ({"base": "USD", "quote": "USD", "mid": "1.000000"}, (404, "unknown_pair")),
        ({"base": "USD", "quote": "MXN", "mid": 18.5}, (422, "invalid_rate")),
        ({"base": "USD", "quote": "MXN", "mid": "0"}, (422, "invalid_rate")),
        ({"base": "USD", "quote": "MXN", "mid": "0.000000"}, (422, "invalid_rate")),
        ({"base": "USD", "quote": "MXN", "mid": "-18.5"}, (422, "invalid_rate")),
        ({"base": "USD", "quote": "MXN", "mid": "1.85e1"}, (422, "invalid_rate")),
        ({"base": "USD", "quote": "MXN", "mid": "18.5000001"}, (422, "invalid_rate")),
        ({"base": "USD", "quote": "MXN", "mid": "1000000.000001"}, (422, "invalid_rate")),
        ({"base": "USD", "quote": "MXN", "mid": " 18.5"}, (422, "invalid_rate")),
        ({"base": "USD", "quote": "MXN"}, (422, "invalid_rate")),
        ({"base": "USD", "mid": "18.5"}, (422, "invalid_request")),
        ({"base": 7, "quote": "MXN", "mid": "18.5"}, (422, "invalid_request")),
    ],
)
async def test_a_malformed_pin_is_refused_and_pins_nothing(
    sim: Sim, body: object, refusal: tuple[int, str]
) -> None:
    response = await sim.anonymous.post("/_control/fx/rates", json=body)

    assert code_of(response) == refusal
    assert (await rate(sim, "USD", "MXN"))["mid"] == "17.250000"


@pytest.mark.parametrize(
    ("pinned", "reverse"),
    [
        ("1000000", "0.000001"),
        ("0.000001", "1000000.000000"),
        ("3", "0.333333"),
        ("0.000003", "333333.333333"),
    ],
)
async def test_the_reverse_of_a_pin_is_rounded_to_six_places(
    sim: Sim, pinned: str, reverse: str
) -> None:
    await sim.control("POST", "/fx/rates", {"base": "USD", "quote": "MXN", "mid": pinned})

    assert (await rate(sim, "MXN", "USD"))["mid"] == reverse
