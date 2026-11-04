from pathlib import Path

from agentco.github.client import FakeGitHub
from agentco.github.models import GhPull, GhReviewComment
from agentco.importer.episodes import ReviewThread

REPO = "acme-org/web"
FIXTURES = Path(__file__).parent.parent / "github" / "fixtures"

# graph.py at commit h1; `switch` spans lines 5-6
GRAPH_AT_H1 = (
    b"class GraphView:\n"
    b"    def load(self, g):\n"
    b"        return g\n"
    b"\n"
    b"    def switch(self, g):\n"
    b"        self.load(g)\n"
)


def pr3() -> GhPull:
    pulls = FakeGitHub.from_fixtures(FIXTURES).pulls(REPO)
    return next(p for p in pulls if p.number == 3)


def _comment(comment_id: int) -> GhReviewComment:
    comments = FakeGitHub.from_fixtures(FIXTURES).review_comments(REPO)
    return next(c for c in comments if c.id == comment_id)


def comment_101() -> GhReviewComment:
    return _comment(101)


def thread_101() -> ReviewThread:
    return ReviewThread(_comment(101), (_comment(102),))


def thread_106() -> ReviewThread:
    return ReviewThread(_comment(106), ())
