# Multimedia fixture corpus

This corpus is entirely synthetic and is licensed under the repository's MIT
license. It contains no third-party audio, images, voices, personal data, or
recorded video. The byte builders live next to the modality tests so fixtures
remain tiny, deterministic, and inspectable in CI.

`corpus.json` is the inventory used for dogfood coverage. Each item points to
the test module that creates and exercises its fixture.

Do not replace these fixtures with downloaded media without recording its
license, source, redistribution permission, and any privacy constraints here.
