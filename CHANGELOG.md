# Changelog

## Unreleased

- Add validated, source-linked account briefs, missing-data handling, progress
  output, explicit owner-authorized approvals/denials, and receipt outcomes.
- Verify clean package consumers over binary HTTP with killed workers, storage
  failures, lost responses and Python/TypeScript checkpoint interoperability.
- Atomically publish and sync private checkpoint files before submission.

## [0.1.3](https://github.com/dx-corp/mono/compare/sdk/deixic/python/v0.1.2...sdk/deixic/python/v0.1.3) (2026-09-19)


### Features

* **code:** release private coding acceptance in 0.10.91 ([#9652](https://github.com/dx-corp/mono/issues/9652)) ([81e8153](https://github.com/dx-corp/mono/commit/81e8153ececc279b216058e7bdc943262d19a912))
* **sdk:** complete the recoverable account brief application ([#9650](https://github.com/dx-corp/mono/issues/9650)) ([efac3fc](https://github.com/dx-corp/mono/commit/efac3fc80aedcac02a119b5d142d53fb4b35230e))

## [0.1.2](https://github.com/dx-corp/mono/compare/sdk/deixic/python/v0.1.1...sdk/deixic/python/v0.1.2) (2026-09-18)


### Features

* **sdk:** add recoverable task results and account briefs ([#9602](https://github.com/dx-corp/mono/issues/9602)) ([2172f17](https://github.com/dx-corp/mono/commit/2172f1772cc919fbe3c7f1cf34fdd6d79d993fb2))
* **sdk:** preserve task identity across Python recovery ([#9585](https://github.com/dx-corp/mono/issues/9585)) ([9ab342b](https://github.com/dx-corp/mono/commit/9ab342bbf6351ed34c8e9d9b53ff002fe939e32f))


### Bug Fixes

* **sdk:** repair Python requests and generated release metadata ([#9583](https://github.com/dx-corp/mono/issues/9583)) ([1e5f136](https://github.com/dx-corp/mono/commit/1e5f136f253ed6afd63fd782caace3a052b010ee))

## [0.1.1](https://github.com/dx-corp/mono/compare/sdk/deixic/python/v0.1.0...sdk/deixic/python/v0.1.1) (2026-09-18)


### Features

* **sdk:** ship public Deixic clients ([#9439](https://github.com/dx-corp/mono/issues/9439)) ([79c826f](https://github.com/dx-corp/mono/commit/79c826ffac8b07361056b1d4d072b3a6dd82e3f6))


### Bug Fixes

* **runtime:** derive runtime admission from capability catalog ([#8990](https://github.com/dx-corp/mono/issues/8990)) ([15b336c](https://github.com/dx-corp/mono/commit/15b336c392c5d36a7637f938baea69f9e165cdbb))


### Chores

* **deps:** bump the cargo group across 1 directory with 2 updates ([#9342](https://github.com/dx-corp/mono/issues/9342)) ([cf94c9a](https://github.com/dx-corp/mono/commit/cf94c9a949e588d68d2e1e54956f8f8e876d5aff))

## 0.1.0

- Publish the first Deixic Python SDK for durable threads, events, messages,
  controls, receipt actions, and binary Connect streaming.
