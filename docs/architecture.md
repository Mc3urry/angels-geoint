# Architecture

## The rule

`core` never imports `adapters`. Enforced by `tests/test_architecture.py`,
which walks the import graph and fails the build.

Everything the core needs to know about a domain arrives through
`PlausibilityModel`: four numbers and a `coverage()` function.

| | Aircraft | Vessels |
|---|---|---|
| `max_speed_mps` | 340 | 26 |
| `max_accel_mps2` | 15 | 1 |
| `expected_report_interval_s` | 1 | 180 |
| `nominal_position_uncertainty_m` | 10 | 50 |

If a fifth field seems necessary, a detector is probably reaching into domain
specifics it should not know about.

## Report vs Observation

The keystone. A `Report` is what a platform says about itself — cooperative,
unauthenticated, defeatable. An `Observation` is what a sensor saw —
independent, and usually carrying no identity at all.

The entire project is the asymmetry between them. Collapsing them into one
"position record" type would make the architecture meaningless.

## Layers

```
adapters  ->  core  ->  analysis
                    ->  inversion
                    ->  api  ->  web
```

Dependencies point inward. `inversion` sits beside `adapters` rather than
inside `aviation` because it consumes core detector output — the orbit
classifier it depends on is domain-blind, and could one day be pointed at
vessels.

## Coordinates, units, time

UTC and SI internally, always. Conversion happens in the adapter and nowhere
else. Knots, nautical miles, feet and local time all try to leak in from source
data; stop them at the edge.

`core/models.py` uses spherical-earth geodesy and depends on nothing outside
the standard library. Accurate to roughly 0.3%, far inside our positional
uncertainties. Heavier machinery lives in `analysis`.

## The targeting boundary

Vehicles, vessels, aircraft, facilities, organisations, and aggregate spatial
patterns are in scope. Identifiable individuals are not.

Four reasons, in the order that actually decides it:

1. **It is technically shallow.** Tracking a platform is a sensing problem —
   clutter statistics, detection theory, fusion under uncertainty. Tracking a
   person is a scraping-and-joining problem. The engineering depth is all on
   one side.
2. **IRB.** Human subjects review is not a formality when the method is
   surveillance of identifiable individuals without consent.
3. **Legal exposure.** Aggregating personal location data, and biometric
   processing especially, is regulated and varies by jurisdiction.
4. **The field draws this line itself.** Every relevant employer operates under
   authorities that distinguish sharply between platforms and persons.

The inversion — detecting surveillance aircraft — stays inside this boundary.
The unit of analysis is the aircraft and the area, never a resident.
