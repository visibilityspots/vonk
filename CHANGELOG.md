# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.2.9] - 2026-10-27
### :wrench: Chores
- [`67bb063`](https://github.com/visibilityspots/vonk/commit/67bb063c87f40c0c2b48fac75d846697b93f9769) - **deps**: bump actions/setup-python from 5 to 7 *(PR [#2](https://github.com/visibilityspots/vonk/pull/2) by [@dependabot[bot]](https://github.com/apps/dependabot))*
- [`9557f43`](https://github.com/visibilityspots/vonk/commit/9557f43238d5b5ed7ef4d3125f35cbd8d5ac686c) - **deps**: bump caldav from 3.4.0 to 3.6.0 *(PR [#4](https://github.com/visibilityspots/vonk/pull/4) by [@dependabot[bot]](https://github.com/apps/dependabot))*
- [`88af79b`](https://github.com/visibilityspots/vonk/commit/88af79bb4c4e54a0d7d705f7632d61f2fab2c41e) - **deps**: bump anthropic from 1.13.0 to 2.3.0 *(PR [#5](https://github.com/visibilityspots/vonk/pull/5) by [@dependabot[bot]](https://github.com/apps/dependabot))*
- [`016ea83`](https://github.com/visibilityspots/vonk/commit/016ea8341581c94c01981c8732d6f72539e22bdf) - **deps-dev**: bump ruff from 0.17.0 to 0.17.2 *(PR [#7](https://github.com/visibilityspots/vonk/pull/7) by [@dependabot[bot]](https://github.com/apps/dependabot))*
- [`d7413f7`](https://github.com/visibilityspots/vonk/commit/d7413f78bdeb274a6ada3c4499ed472ec805fbc5) - **deps**: bump actions/checkout from 6 to 7 *(PR [#3](https://github.com/visibilityspots/vonk/pull/3) by [@dependabot[bot]](https://github.com/apps/dependabot))*
- [`560c5e5`](https://github.com/visibilityspots/vonk/commit/560c5e517a2d92796db57d54902f22256e19d0d7) - **deps**: bump starlette from 1.7.0 to 1.9.0 *(PR [#6](https://github.com/visibilityspots/vonk/pull/6) by [@dependabot[bot]](https://github.com/apps/dependabot))*
- [`1624cf0`](https://github.com/visibilityspots/vonk/commit/1624cf0a118d621e406074b56cbc4bd7e5e24429) - release v0.2.9 *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.8] - 2026-10-26
### :sparkles: New Features
- [`0c61fb5`](https://github.com/visibilityspots/vonk/commit/0c61fb576373e7893ae6e2121e24783dd24078f9) - **feed**: restyle the subscribe page *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :bug: Bug Fixes
- [`2311764`](https://github.com/visibilityspots/vonk/commit/231176431aa29f5036d34cbb438f2715bd318a48) - **docker**: remove pip from the runtime image *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :wrench: Chores
- [`6bf0c7e`](https://github.com/visibilityspots/vonk/commit/6bf0c7ecfef1381ea33718a14e671a3838e6ef74) - remove the Nomad job specs from the public repository *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.7] - 2026-10-26
### :sparkles: New Features
- [`61aa73b`](https://github.com/visibilityspots/vonk/commit/61aa73bb8ba56b1f1cec188211cfc44232b191ff) - **feed**: keep the history, limited to the current scout year *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.6] - 2026-10-26
### :bug: Bug Fixes
- [`ae6dcb4`](https://github.com/visibilityspots/vonk/commit/ae6dcb4b9fa9ebbe2bae90da785d90404c5ec1ad) - cross-check new date layouts, version from pyproject, Android link *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.5] - 2026-10-26
### :bug: Bug Fixes
- [`2e5a233`](https://github.com/visibilityspots/vonk/commit/2e5a23360dc5b3a77da5c04e2cf1521fdc869983) - **normalize**: read dates without a month and d-m dates *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :wrench: Chores
- [`340fce9`](https://github.com/visibilityspots/vonk/commit/340fce9e7bb13ed2e9d65d54ab5f87821cb5f5be) - **feed**: serve the ICS feed from gh-pages in this repository *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.4] - 2026-10-26
### :sparkles: New Features
- [`eb9e590`](https://github.com/visibilityspots/vonk/commit/eb9e590a2b5b11df5b8b94373dd41e38d6857ddd) - **feed**: publish a public ICS feed per tak to GitHub Pages *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.3] - 2026-10-26
### :bug: Bug Fixes
- [`7de4b29`](https://github.com/visibilityspots/vonk/commit/7de4b29b22f744694d0c4d742ca3685e9737aadb) - **poll**: read image-only vonk PDFs with OCR *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.2] - 2026-10-11
### :bug: Bug Fixes
- [`93d50fa`](https://github.com/visibilityspots/vonk/commit/93d50fa40e18a3f8f8a246a91fe94e6b8c8aa962) - filter the CalDAV REPORT on the own UID domain server-side *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.1] - 2026-10-11
### :bug: Bug Fixes
- [`7fa6534`](https://github.com/visibilityspots/vonk/commit/7fa65341f285f6d2ee448f0b172bfb549e45cf7d) - re-extract seen posts after a prompt version bump *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.0] - 2026-10-11
### :sparkles: New Features
- [`4058ae3`](https://github.com/visibilityspots/vonk/commit/4058ae3da614b24c137e4c2e23cf6ec7b49c6058) - short event titles "Scouts <name> - <summary>" *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :bug: Bug Fixes
- [`3445595`](https://github.com/visibilityspots/vonk/commit/344559520035c7e49daff7267ccda8b22b4c62cd) - **nomad**: read poll secrets from the poll job's Vault path *(commit by [@visibilityspots](https://github.com/visibilityspots))*

[v0.2.0]: https://github.com/visibilityspots/vonk/compare/v0.1.0...v0.2.0
[v0.2.1]: https://github.com/visibilityspots/vonk/compare/v0.2.0...v0.2.1
[v0.2.2]: https://github.com/visibilityspots/vonk/compare/v0.2.1...v0.2.2
[v0.2.3]: https://github.com/visibilityspots/vonk/compare/v0.2.2...v0.2.3
[v0.2.4]: https://github.com/visibilityspots/vonk/compare/v0.2.3...v0.2.4
[v0.2.5]: https://github.com/visibilityspots/vonk/compare/v0.2.4...v0.2.5
[v0.2.6]: https://github.com/visibilityspots/vonk/compare/v0.2.5...v0.2.6
[v0.2.7]: https://github.com/visibilityspots/vonk/compare/v0.2.6...v0.2.7
[v0.2.8]: https://github.com/visibilityspots/vonk/compare/v0.2.7...v0.2.8
[v0.2.9]: https://github.com/visibilityspots/vonk/compare/v0.2.8...v0.2.9
