from pureiron_updater import __version__


def test_version_is_semver() -> None:
    major, minor, patch = (int(part) for part in __version__.split("."))
    assert (major, minor, patch) >= (0, 1, 0)
