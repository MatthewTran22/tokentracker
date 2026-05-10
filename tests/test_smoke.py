def test_imports():
    """Verify package imports under test env (AUTH_SECRET seeded by conftest)."""
    from tokentracker import main  # noqa: F401
    from tokentracker import routes  # noqa: F401
    from tokentracker import security  # noqa: F401
