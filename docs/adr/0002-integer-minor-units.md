# 0002. Integer minor units for money

**Status:** accepted

## Context

A wallet adds and subtracts amounts millions of times and must never lose or create a
fraction. Binary floating point cannot represent `0.1`. JSON numbers are floats in most
clients and lose precision above 2^53. The assets have different scales: `USD` has 2 decimal
places and `USDC` has 6, and tokens with 18 exist.

## Decision

- Inside the system an amount is a Python `int`: a count of the asset's smallest unit.
- In the database it is `NUMERIC(38,0)`, through the `MinorUnits` column type, which refuses
  to bind anything that is not an `int` and refuses a fractional value on the way back.
- At the API boundary it is a decimal string in major units next to an asset code. A JSON
  number is refused. A string with more decimal places than the asset has is refused, not
  rounded.
- `corridor.platform.money` is the only code that converts between the two forms.

## Consequences

- Arithmetic is exact. Two amounts are equal when their integers are equal.
- `BIGINT` would have been faster and would overflow at about 9.2 units of an 18-decimal
  token. `NUMERIC(38,0)` holds any amount the system can meet.
- Anything that divides needs an explicit rounding rule. There are two: an FX quote rounds
  the buy amount down, and a movement's USD value for limits rounds up.
- Clients must send strings. A client that sends `12.5` gets a `422`, which is deliberate.
- Fees are integer arithmetic on basis points, rounded down, with a per-asset minimum.
