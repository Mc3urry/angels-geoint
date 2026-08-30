# ANGELS

**angels** *(n.)* — radar returns with no attributable source. Something is
present; the sensor cannot say what.

## The question

Almost everything we know about the movement of ships and aircraft comes from
**cooperative reporting** — the platform announces its own position, and
everyone downstream treats that announcement as fact. None of these protocols
authenticate. Every one can be switched off, spoofed, or given someone else's
identity, and the infrastructure keeps believing it.

Against that sits **independent sensing**: instruments that observe whether or
not the target consents. Radar satellites see hulls. Ground receiver networks
triangulate a transmitter's real position regardless of what it claims.

The intelligence product is the **discrepancy**. What the sensor saw, minus
what the subject admitted.

> **Does non-cooperative behaviour concentrate at governance discontinuities?**
> Boundaries where enforcement authority changes — EEZ edges, marine protected
> area borders, flight information region lines — create incentive gradients.
> The hypothesis is that platforms go dark disproportionately near them, and
> that distance-to-boundary predicts the rate.

Two domains, one core. If the effect appears in both shipping and aviation,
that is a much stronger finding than either alone.

## Architecture

```
adapters/     the only domain-specific code
    |         translate a native feed into Tracks and Observations,
    v         and supply a PlausibilityModel
core/         written once, domain-blind
    |         gaps, kinematics, identity, matching, loiter, orbits, rendezvous
    v
analysis/     spatial statistics over the resulting events
```

One rule, enforced by `tests/test_architecture.py`: **`core` never imports
`adapters`.** Everything the core needs to know about a domain arrives through
four numbers and a `coverage()` function.

The other load-bearing idea lives in `core/models.py`: a `Report` and an
`Observation` are different types. Merge them and the architecture becomes
pointless.

## Quickstart

```bash
make dev          # editable install with test deps
make test         # should be green immediately
make serve        # API on :8000
make web          # static front end on :5173
```

Then copy `.env.example` to `.env` and fill in your OpenSky credentials.

## Status

| Phase | | |
|---|---|---|
| 1 | Core model, aviation ingest, map | in progress |
| 2 | Detectors, coverage, the inversion | |
| 3 | Maritime adapter — the generality test | |
| 4 | Spatial analysis | |
| 5 | Chronolocation | |
| 6 | Harden and ship | |

## Scope

Targets are **platforms and institutions, never persons** — vessels, aircraft,
facilities, organisations, and aggregate spatial patterns. That boundary is
deliberate, and the reasoning is in `docs/architecture.md`.

## License

MIT.
