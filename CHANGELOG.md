# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.2.8] - 2026-10-26
### :sparkles: New Features
- [`a7c3a77`](https://github.com/visibilityspots/vonk/commit/a7c3a77592e1a5accff424132ec632a90e95d7df) - **feed**: restyle the subscribe page *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :bug: Bug Fixes
- [`3dbc853`](https://github.com/visibilityspots/vonk/commit/3dbc8539ad50b1f57a39aff3a8235d2387e75141) - **docker**: remove pip from the runtime image *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :wrench: Chores
- [`f84b928`](https://github.com/visibilityspots/vonk/commit/f84b9286efd16416c10b9a371bc2bc85336c466d) - **nomad**: bump vonk jobs to v0.2.7 *(commit by [@visibilityspots](https://github.com/visibilityspots))*
- [`bf17af5`](https://github.com/visibilityspots/vonk/commit/bf17af57cd50c71c04a60f72036001d1cec6b40e) - remove the Nomad job specs from the public repository *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.7] - 2026-10-26
### :sparkles: New Features
- [`c71afae`](https://github.com/visibilityspots/vonk/commit/c71afaea1f95bc3c7c5cbbad50535e03918246ae) - **feed**: keep the history, limited to the current scout year *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :wrench: Chores
- [`ae05d16`](https://github.com/visibilityspots/vonk/commit/ae05d16f542989af097fc7d90a99a2ca66a579e3) - **nomad**: bump vonk jobs to v0.2.6 *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.6] - 2026-10-26
### :bug: Bug Fixes
- [`2997edf`](https://github.com/visibilityspots/vonk/commit/2997edfff4f5b52bb3af214e0dbbf3e5167245de) - cross-check new date layouts, version from pyproject, Android link *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :wrench: Chores
- [`f6e3a52`](https://github.com/visibilityspots/vonk/commit/f6e3a52037d24186cfd3b0563db6892b02355ce4) - **nomad**: bump vonk jobs to v0.2.5 *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.5] - 2026-10-26
### :bug: Bug Fixes
- [`40e017e`](https://github.com/visibilityspots/vonk/commit/40e017ef2c5e13ce750a26f6e293ff345d8db87e) - **normalize**: read dates without a month and d-m dates *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :wrench: Chores
- [`caedce9`](https://github.com/visibilityspots/vonk/commit/caedce98dcb81f9ab01f35fc7e6288703dfb5e32) - **feed**: serve the ICS feed from gh-pages in this repository *(commit by [@visibilityspots](https://github.com/visibilityspots))*
- [`287ea08`](https://github.com/visibilityspots/vonk/commit/287ea08bc95c57c5e1662a50f16983b1fb7020fc) - **nomad**: bump vonk jobs to v0.2.4 *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.4] - 2026-10-26
### :sparkles: New Features
- [`3b71728`](https://github.com/visibilityspots/vonk/commit/3b717285363eed05b2d1a011464355e627fb0f46) - **feed**: publish a public ICS feed per tak to GitHub Pages *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.3] - 2026-10-26
### :bug: Bug Fixes
- [`5447024`](https://github.com/visibilityspots/vonk/commit/544702465fd37b72bf067d7040c5ee8114d2168b) - **poll**: read image-only vonk PDFs with OCR *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.2] - 2026-10-11
### :bug: Bug Fixes
- [`5497432`](https://github.com/visibilityspots/vonk/commit/5497432a0c0fe81295535ce9b95bc9b0c6ced8af) - filter the CalDAV REPORT on the own UID domain server-side *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.1] - 2026-10-11
### :bug: Bug Fixes
- [`557d15c`](https://github.com/visibilityspots/vonk/commit/557d15cdd8ce0385db664de11c208c25e568ef29) - re-extract seen posts after a prompt version bump *(commit by [@visibilityspots](https://github.com/visibilityspots))*


## [v0.2.0] - 2026-10-11
### :sparkles: New Features
- [`a90043f`](https://github.com/visibilityspots/vonk/commit/a90043f04f0dc72b4ca0cbe49e16869ecf6e2d9d) - short event titles "Scouts <name> - <summary>" *(commit by [@visibilityspots](https://github.com/visibilityspots))*

### :bug: Bug Fixes
- [`4f2fe9f`](https://github.com/visibilityspots/vonk/commit/4f2fe9fd712a8115087b7aab3d6b19a50cbba8e0) - **nomad**: read poll secrets from the poll job's Vault path *(commit by [@visibilityspots](https://github.com/visibilityspots))*

[v0.2.0]: https://github.com/visibilityspots/vonk/compare/v0.1.0...v0.2.0
[v0.2.1]: https://github.com/visibilityspots/vonk/compare/v0.2.0...v0.2.1
[v0.2.2]: https://github.com/visibilityspots/vonk/compare/v0.2.1...v0.2.2
[v0.2.3]: https://github.com/visibilityspots/vonk/compare/v0.2.2...v0.2.3
[v0.2.4]: https://github.com/visibilityspots/vonk/compare/v0.2.3...v0.2.4
[v0.2.5]: https://github.com/visibilityspots/vonk/compare/v0.2.4...v0.2.5
[v0.2.6]: https://github.com/visibilityspots/vonk/compare/v0.2.5...v0.2.6
[v0.2.7]: https://github.com/visibilityspots/vonk/compare/v0.2.6...v0.2.7
[v0.2.8]: https://github.com/visibilityspots/vonk/compare/v0.2.7...v0.2.8
