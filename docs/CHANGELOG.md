# Changelog

## [0.4.0](https://github.com/shixuan/crawl-me-maybe/compare/v0.3.0...v0.4.0) (2026-08-25)


### ⚠ BREAKING CHANGES

* **cli:** exit non-zero when the crawl was refused

### Features

* **cli:** exit non-zero when the crawl was refused ([48b43e4](https://github.com/shixuan/crawl-me-maybe/commit/48b43e4596bc11dd195ef19df24c609bc6813f63))


### Fixes

* remove some dead code ([1e06447](https://github.com/shixuan/crawl-me-maybe/commit/1e064474359bb45e9a74ce3f522f3cac93b865fd))
* **scheduler:** feed the ranker what the analyzer established ([e5a2bd0](https://github.com/shixuan/crawl-me-maybe/commit/e5a2bd01ff375e249e4c5d2c254dd4aac4c1455f))
* **scheduler:** settle the fetches in the air when a run is interrupted ([94d3a9a](https://github.com/shixuan/crawl-me-maybe/commit/94d3a9ae892de97cf3b7029c981bdfbec920f019))


### Changed

* **llm:** stop asking for three fields nothing reads ([7f4ae05](https://github.com/shixuan/crawl-me-maybe/commit/7f4ae0519e0af0c2da5bda28b43fbc8089072a94))
* **schemas:** keep only the history fields the prompt reads ([2e0cef7](https://github.com/shixuan/crawl-me-maybe/commit/2e0cef7cc9f594ba79366cb4741af22a0e2bd010))
