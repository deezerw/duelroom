"""PUCT tree search on the real engine (Stage 16 step 3, arm a).

AlphaZero's search without the learned rules: the engine (ygorl's
DuelSession with snapshots) is the model, so a node is a position reached by
replaying its path of action indices from the root snapshot. What a network
would provide comes in as two functions:

  prior(point, session) -> [p per legal action]   (default: uniform)
  value(point, session) -> v in [-1, 1] for engine player 0
                                                   (default: 0 until the duel ends)

A finished duel is valued exactly (+1 player 0 won, -1 lost). `goal(session)`
may end a simulation early with its own value (e.g. "player 1 won" for a kill
search that stops at the win). Each decision is taken by `point.player`, and a
node's choice maximises that player's value: the tree covers both seats, the
turn player's moves and the defender's answers alike.

Pure Python over ygorl; no torch here (the server has none). Measured
2026-11-19: ~4,900 engine decisions/s on one core with snapshot/restore.
"""

import math
import random
from dataclasses import dataclass, field


@dataclass
class Node:
    player: int                      # who decides here (engine player)
    priors: list                     # P(a) over the legal actions
    n: list = field(default_factory=list)   # visits per action
    w: list = field(default_factory=list)   # summed value per action, player 0's view
    children: dict = field(default_factory=dict)
    visits: int = 0

    def __post_init__(self):
        k = len(self.priors)
        self.n = [0] * k
        self.w = [0.0] * k


def uniform(point, session, observer=None):
    k = len(point.actions)
    return [1.0 / k] * k


def outcome_only(point, session, observer=None):
    return 0.0


class Search:
    """Search from `session`'s current position (it is snapshotted and left
    there). `c_puct` as AlphaZero; `max_depth` caps a simulation's descent."""

    def __init__(self, session, prior=uniform, value=outcome_only, goal=None,
                 c_puct=1.5, max_depth=400, seed=0, observer=None):
        """`observer`: a ygorl PointObserver that has seen every decision up
        to the root (a network reads the event history, not just the board).
        Each simulation carries a copy of it down its path, and `prior` /
        `value` are then called with it as a third argument."""
        self.session = session
        self.root_snap = session.snapshot()
        self.observer = observer
        self._obs = None
        self.prior, self.value, self.goal = prior, value, goal
        self.c, self.max_depth = c_puct, max_depth
        self.rng = random.Random(seed)
        self.root = None
        self.steps = 0

    def _terminal(self):
        """Exact value if the duel ended (or the goal says so), else None."""
        s = self.session
        if self.goal is not None:
            g = self.goal(s)
            if g is not None:
                return g
        if s.done or s.point is None:
            winner = s.tracker.engine_winner
            if winner is None:
                return 0.0
            return 1.0 if winner == 0 else -1.0
        return None

    def _expand(self):
        point = self.session.point
        if self._obs is None:
            return (Node(point.player, list(self.prior(point, self.session))),
                    float(self.value(point, self.session)))
        self._obs.observe(point)
        return (Node(point.player, list(self.prior(point, self.session, self._obs))),
                float(self.value(point, self.session, self._obs)))

    def _select(self, node):
        sign = 1.0 if node.player == 0 else -1.0
        sq = math.sqrt(node.visits + 1)
        best, best_u = 0, -1e30
        for a, p in enumerate(node.priors):
            q = sign * node.w[a] / node.n[a] if node.n[a] else 0.0
            u = q + self.c * p * sq / (1 + node.n[a])
            if u > best_u + 1e-12 or (abs(u - best_u) <= 1e-12 and self.rng.random() < 0.5):
                best, best_u = a, u
        return best

    def simulate(self):
        import copy

        s = self.session
        s.restore(self.root_snap)
        self._obs = None
        if self.observer is not None:
            o = self.observer
            # the card database, vocab and encoder never change in a search (and the
            # database cannot be copied): shared, the event history copied
            shared = {id(x): x for x in (getattr(o, "cards", None), getattr(o, "vocab", None),
                                         getattr(o, "encoder", None)) if x is not None}
            self._obs = copy.deepcopy(o, shared)
        if self.root is None:
            t = self._terminal()
            if t is not None:
                return t
            self.root, v = self._expand()
            return v
        node, path = self.root, []
        for _depth in range(self.max_depth):
            a = self._select(node)
            path.append((node, a))
            if self._obs is not None:          # every point is observed once, then decided
                point = s.point
                self._obs.observe(point)
                self._obs.on_decision(point, a)
            s.act(a)
            self.steps += 1
            t = self._terminal()
            if t is not None:
                v = t
                break
            child = node.children.get(a)
            if child is None:
                child, v = self._expand()
                node.children[a] = child
                break
            node = child
        else:
            v = 0.0
        for nd, a in path:
            nd.n[a] += 1
            nd.w[a] += v
            nd.visits += 1
        return v

    def run(self, simulations):
        for _ in range(simulations):
            self.simulate()
        self.session.restore(self.root_snap)
        return self

    def policy(self):
        """Visit share per root action (AlphaZero's training target)."""
        tot = sum(self.root.n) or 1
        return [x / tot for x in self.root.n]

    def best(self):
        """The most visited root action."""
        return max(range(len(self.root.n)), key=lambda a: (self.root.n[a], self.root.priors[a]))

    def principal_line(self, limit=200):
        """Most-visited actions from the root down the tree."""
        out, node = [], self.root
        while node is not None and node.visits and len(out) < limit:
            a = max(range(len(node.n)), key=lambda i: node.n[i])
            out.append(a)
            node = node.children.get(a)
        return out
