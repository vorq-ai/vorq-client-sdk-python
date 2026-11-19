# Changelog

All notable changes to the `vorq` package are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

Every release bumps the patch version (`0.2.0`, `0.2.1`, …), so `vorq~=0.2.0` takes each one
and a plain `pip install -U vorq` always gets the newest. A breaking change bumps the minor
(`0.3.0`).

## [0.3.0] — Unreleased

### Changed

- **Breaking:** jobs are paid from a prepaid balance in the job registry. A submission no
  longer signs a payment per job: the complete `POST /v1/jobs` body carries the quoted `amount`
  and no `auth_sig`, and a batch line likewise. The quote carries `balance`, `deposit` and
  `deposit_fee`, and its `authorization` block is gone.
- **Breaking:** `Signer.sign_payment_authorization` is replaced by
  `sign_deposit_authorization(amount, nonce, valid_before, ctx)`, a deposit into the balance
  with a random 32-byte nonce and a one-hour deadline. A custom signer implements it and
  `sign_withdraw`.
- When the quote shows the balance short, `submit` deposits the shortfall first with one
  gasless signature, and `batches.submit` deposits the batch's shortfall once before the
  upload. The wallet needs USDC and no ETH.
- An order that expires unclaimed costs nothing.

### Added

- `Client.balance()`, `Client.deposit(amount)` and `Client.withdraw(amount)`, amounts as USD
  decimal strings.
- `Client(auto_deposit=...)`, default `True`. With `False`, a short balance raises the new
  `InsufficientBalance` (`.balance`, `.required`, `.shortfall`) and nothing is posted.
- `Signer.sign_withdraw(amount, issued_at, ctx)`, signing `Withdraw(uint128 amount,uint64 issuedAt)`
  on the job registry's domain.
- `VorqError.code` (`str | None`), the wire `error.code`. A withdrawal over `available` is a
  `StateConflictError` with `.code == "insufficient_balance"`.

## [0.2.2] — 2026-11-18

### Changed

- **Breaking:** text and embedding `units_in` is the byte length of the canonical JSON input
  (was one unit per four bytes). It is a ceiling: settlement bills the model's own prompt count
  under it and refunds the rest.
- **Breaking:** `.cost` is the chain's charge off the job row's settled counts,
  `((prompt_tok − cached_tok) × rate_in + cached_tok × rate_in_cached + completion_tok × rate_out) / 1,000,000`,
  for every result type. A job row or batch line without `prompt_tok`, `cached_tok` and
  `completion_tok` is refused.

### Added

- `prompt_tok`, `cached_tok`, `completion_tok` and `rate_in_cached` on `TextResult`, `MediaResult`
  and `EmbeddingResult`.

## [0.2.1] — 2026-11-18

### Changed

- The network runs on Base mainnet. The docs describe funding a wallet with USDC on Base, and
  the examples use `moonshotai/kimi-k3.1-pro:mxfp4`. Model ids carry a precision suffix.
  No code change.

## [0.2.0] — 2026-10-19

The first stable release.

### Changed

- **Breaking:** `batches.submit()`'s `providers=` is an allow-list. Lines are planned only onto
  the listed providers, cheapest first; `provider` in a line's body restricts that line to
  exactly that provider and overrides `providers=`. A restricted line never rests: if any cannot
  be placed, the whole batch raises `ValidationError` before anything is signed. An empty list is
  refused. Resting lines are no longer spread round-robin: an unrestricted line the plan cannot
  place rests as an open order, and a client without `verifier=` is refused before anything is
  sealed.
- A batch reads each distinct provider's registry record and each model's id once per submit,
  not once per line.

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
