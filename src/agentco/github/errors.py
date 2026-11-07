class GitHubError(RuntimeError):
    pass


class InvalidRef(ValueError):
    """A commit ref that is not a plain sha; it never reaches `gh`."""
