# Changelog

## [0.5.0](https://github.com/shixuan/crawl-me-maybe/compare/v0.4.1...v0.5.0) (2026-09-12)


### Features

* **analyzer:** drop HUB and the links it endorsed ([9115ab5](https://github.com/shixuan/crawl-me-maybe/commit/9115ab57589453c0f346b6bdf8a562f70be03151))
* **analyzer:** read the dates a page gives for what it describes ([7d947a8](https://github.com/shixuan/crawl-me-maybe/commit/7d947a89e488594e8fcf87d9d90d200ad0e4e17c))
* **analyzer:** stop at the verdict on a page it discards ([4a81c69](https://github.com/shixuan/crawl-me-maybe/commit/4a81c694543e385991e776f6e87dd6d49abfa351))
* **cli:** group results by whether they have run out ([d0a8ed8](https://github.com/shixuan/crawl-me-maybe/commit/d0a8ed85fa696e8f123698663b7d506b3ccd25b8))
* **cli:** take a window for how far ahead still counts as open ([5b92ace](https://github.com/shixuan/crawl-me-maybe/commit/5b92acedc5cb0959642a3cf8b408ae57890fa7ee))
* **dashboard:** group results by when they run ([3cae049](https://github.com/shixuan/crawl-me-maybe/commit/3cae0498260c34e7e9025fcf7a2b982f9c984d91))
* **llm:** name the thinking effort once, translate it per model ([169b4a0](https://github.com/shixuan/crawl-me-maybe/commit/169b4a04989e18be6f24b6c186dcca44ff31a6b4))
* **pioneer:** propose extra seeds and verify them ([1fa5e4c](https://github.com/shixuan/crawl-me-maybe/commit/1fa5e4c8e3e2bb242ad8686a2ccae188724bfabb))
* seed enhancement ([42a5ac2](https://github.com/shixuan/crawl-me-maybe/commit/42a5ac2442bf74478376839208851250596e727b))
* support date window ([f91feb5](https://github.com/shixuan/crawl-me-maybe/commit/f91feb5918a27b6c8396f895b028e8ec6d79d7df))


### Fixes

* **cli:** say how much of each proposed seed was read ([ab9f155](https://github.com/shixuan/crawl-me-maybe/commit/ab9f1553c4202c33e5da7293f88193eb87deb7cf))
* **cli:** split the token bill by stage and fix the report's order ([40e8ad7](https://github.com/shixuan/crawl-me-maybe/commit/40e8ad7082557871eddd65e356e342f8d23a5297))
* **dashboard:** declared verdict order ([e1a1a56](https://github.com/shixuan/crawl-me-maybe/commit/e1a1a56ce0bea8277bd49e6bfedbc8ab3a0eeafa))
* **dashboard:** stop the controls reflowing and the date drifting ([8d3c257](https://github.com/shixuan/crawl-me-maybe/commit/8d3c257e49575d2137a51d48996cdee11cb242bc))
* **digest:** drop a block that only repeats the one before it ([2d326fe](https://github.com/shixuan/crawl-me-maybe/commit/2d326fed9b2c594cd56074069e03e4faa3d3bdea))
* **digest:** keep the answer that has the posts ([43a8a2a](https://github.com/shixuan/crawl-me-maybe/commit/43a8a2ad3e2469e66218f2df79109e45e4699535))
* **llm:** think less on an empty reply ([d22d52b](https://github.com/shixuan/crawl-me-maybe/commit/d22d52bc40ec01671586c5bd94731fedf60d9176))
* **logging:** keep startup lines until the run file exists ([57c3538](https://github.com/shixuan/crawl-me-maybe/commit/57c3538721bdd75a7656ff0a7f01dcbaa12d9aec))
* **logging:** say every item at INFO, count it at DEBUG ([7036eee](https://github.com/shixuan/crawl-me-maybe/commit/7036eee46eb64e54fb5ad922582be57a3a6f63fb))
* **pioneer:** age a waiting item once, not every pass ([8549cde](https://github.com/shixuan/crawl-me-maybe/commit/8549cdec472e6ed684bc6a49d749e2f00d6a771f))
* **pioneer:** ask a proposal only what one fetch can settle ([85b19be](https://github.com/shixuan/crawl-me-maybe/commit/85b19bef96a555e0e80502f6dceaee453d9d5c48))
* **pioneer:** show the ranker what analysis found, not the head tag ([d4ed97b](https://github.com/shixuan/crawl-me-maybe/commit/d4ed97b57e275133b735c274a27d8ec15ead9953))
* **ranker:** judge the analyzer's goal ([7a44e6a](https://github.com/shixuan/crawl-me-maybe/commit/7a44e6a98606d8d7e8fd54e1528939d2fd996e23))
* **scheduler:** end the run when a pump dies ([e55f5f4](https://github.com/shixuan/crawl-me-maybe/commit/e55f5f496b08cb2b7148f8dc95a87d7adcfa96af))
* **scheduler:** make a stop mean what it says ([cd87a4b](https://github.com/shixuan/crawl-me-maybe/commit/cd87a4bb1cf7c0c3837bf8a06ea9bfb68c2e66f3))
* **scheduler:** retire a source, not the whole run ([4192314](https://github.com/shixuan/crawl-me-maybe/commit/4192314a5251f17f76e28d49e81ae3549e6a10e7))
* **scheduler:** stop fetching past what analysis can take ([94fe559](https://github.com/shixuan/crawl-me-maybe/commit/94fe55910a00582235c58e540f13b9245920102c))
* **storage:** keep the dates an analysis read ([29c045c](https://github.com/shixuan/crawl-me-maybe/commit/29c045c00d36988950c05d2ccdba17314eebd794))


### Changed

* **logging:** make INFO a sentence, not a measurement ([0e99450](https://github.com/shixuan/crawl-me-maybe/commit/0e99450055fda29761e9f1a38ed660b003854725))
* one shape for counting, one rule for logging ([1f1bf9f](https://github.com/shixuan/crawl-me-maybe/commit/1f1bf9f63349ad0bc0edd0924e798b3675a5b37c))
* **state:** one funnel per seed, not five counters ([5821fb6](https://github.com/shixuan/crawl-me-maybe/commit/5821fb6082208644b9e6d367c3220ce94f6bd524))
* **state:** one record per page, not four maps ([227f887](https://github.com/shixuan/crawl-me-maybe/commit/227f8872eb13ae2f8725fdb7d6a2bc13dff18de3))
* **state:** split the run's state by who reads it ([0f925db](https://github.com/shixuan/crawl-me-maybe/commit/0f925db7cf1494266979e74bf70b6a183081db24))


### Performance

* save token usage ([3f7eba2](https://github.com/shixuan/crawl-me-maybe/commit/3f7eba26d9ffe35fefe9f495fe3c2faacede6445))

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
