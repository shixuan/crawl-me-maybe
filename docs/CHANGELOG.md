# Changelog

## [0.4.1](https://github.com/shixuan/crawl-me-maybe/compare/v0.4.0...v0.4.1) (2026-09-01)


### Fixes

* **cli:** end the session wait when the window closes ([629d3ab](https://github.com/shixuan/crawl-me-maybe/commit/629d3ab8ab5ec2d060de8cbdb316174e11727d06))
* **cli:** say how to fix a refused login ([8a2ea4a](https://github.com/shixuan/crawl-me-maybe/commit/8a2ea4a22f906b0e36905e87f22e4ebac346bc29))
* **config:** default logs to the format a person reads ([86bd8aa](https://github.com/shixuan/crawl-me-maybe/commit/86bd8aade425baaf90a942a0fca1f95729251333))
* make crawl session usable ([8d37eaf](https://github.com/shixuan/crawl-me-maybe/commit/8d37eafdf838b3fc532106c219c8299812b9ce97))

## [0.4.0](https://github.com/shixuan/crawl-me-maybe/compare/v0.3.1...v0.4.0) (2026-08-31)


### Features

* **dashboard:** filter by whether one named field is there ([96e4efd](https://github.com/shixuan/crawl-me-maybe/commit/96e4efd8c83eddec138edb9691c056e60a9020db))
* **digest:** let one crawl move between a platform and the open web ([9668280](https://github.com/shixuan/crawl-me-maybe/commit/966828022d58ebd38435055000fb68563dfb034f))
* **digest:** page through a listing ([09ca016](https://github.com/shixuan/crawl-me-maybe/commit/09ca016df59d10dca008514535f77bc12bdbaa4a))
* **digest:** read reddit, and refuse it without a browser ([99771e5](https://github.com/shixuan/crawl-me-maybe/commit/99771e5360194b7fe01463fa9218ed33523ed5f9))
* let one crawl cross between platforms and the open web ([4d4c306](https://github.com/shixuan/crawl-me-maybe/commit/4d4c30619d5f58db24f99c19550475bb91bb3f96))
* page through a listing ([34eb9fd](https://github.com/shixuan/crawl-me-maybe/commit/34eb9fdbb81b921ea3bf9455cf66e5c9eb325b99))
* **pioneer:** show the ranker how old a candidate is ([ad4d872](https://github.com/shixuan/crawl-me-maybe/commit/ad4d872f53057caf241d00febaa8d3e0ca554a54))
* read reddit ([5f0c304](https://github.com/shixuan/crawl-me-maybe/commit/5f0c304b387491e8e0669f6566b4c514f330e91a))


### Fixes

* **cli:** state the time window in force ([710f0c8](https://github.com/shixuan/crawl-me-maybe/commit/710f0c81c8d3bedcf76f5fb271b5d6f3528f3f65))
* **config:** state the crawler's own name once, without a version to rot ([18df8fc](https://github.com/shixuan/crawl-me-maybe/commit/18df8fc031744990f125477708c37f0850681fa5))
* **pioneer:** obey robots.txt ([dee0755](https://github.com/shixuan/crawl-me-maybe/commit/dee07550fcee40ce687fc0cd50a131ac4d3b3069))
* **storage:** keep one bad statement from hanging the close ([2831a1f](https://github.com/shixuan/crawl-me-maybe/commit/2831a1ff1636a92e77e099fab32ae203097760b3))


### Changed

* drop three fields nothing ever filled and a semaphore nothing awaited ([f8f4abd](https://github.com/shixuan/crawl-me-maybe/commit/f8f4abd7ced19df13d12bb2afc03770207007549))

## [0.3.1](https://github.com/shixuan/crawl-me-maybe/compare/v0.3.0...v0.3.1) (2026-08-25)


### Fixes

* **cli:** exit non-zero when the crawl was refused ([77adf54](https://github.com/shixuan/crawl-me-maybe/commit/77adf54eeada70ed7b7a65fdc152cba3541eb253))
* remove some dead code ([7cbd8e8](https://github.com/shixuan/crawl-me-maybe/commit/7cbd8e8c77ae768c78cb0fda3eb96880316c2feb))
* **scheduler:** feed the ranker what the analyzer established ([e5a2bd0](https://github.com/shixuan/crawl-me-maybe/commit/e5a2bd01ff375e249e4c5d2c254dd4aac4c1455f))
* **scheduler:** settle the fetches in the air when a run is interrupted ([75cb6fb](https://github.com/shixuan/crawl-me-maybe/commit/75cb6fb279f9b6ad6e8e76cff0aa536afc122415))


### Changed

* **llm:** stop asking for three fields nothing reads ([7f4ae05](https://github.com/shixuan/crawl-me-maybe/commit/7f4ae0519e0af0c2da5bda28b43fbc8089072a94))
* **schemas:** keep only the history fields the prompt reads ([2e0cef7](https://github.com/shixuan/crawl-me-maybe/commit/2e0cef7cc9f594ba79366cb4741af22a0e2bd010))
