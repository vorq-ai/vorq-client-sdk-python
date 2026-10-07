# Changelog

All notable changes to the `vorq` package are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0rc3] — 2026-10-07

### Changed

- Waiting on a batch, or on a single `24h` job, now polls once a minute for the first 15
  minutes of the wait, every 3 minutes for the rest of the first hour, and every 10 minutes
  after that. It used to poll once a minute throughout. Windows under 24 hours are unchanged.

## [0.1.0rc2] — 2026-10-07

### Changed

- **Breaking:** `rate_in` / `rate_out` are now `max_rate_in` / `max_rate_out`, on `submit()`,
  on batch lines and in the OpenAI transport's `vorq` block. The old names are not accepted.
- The two are ceilings: an order signs the ask of the first provider within them and never
  more. Each is optional, and a side left out has no ceiling.
- An order no provider is within rests at its ceilings. With one ceiling named, the other side
  rests at the market rate, the cheapest live ask's. It used to be signed as zero, which no
  provider claims.
- Every batch line is planned within its ceilings before sealing; `providers` spreads only the
  lines that rest.

## [0.1.0rc1] — 2026-10-01

Initial public release.
