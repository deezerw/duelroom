"""A ygorl policy checkpoint as skills/mcts.py's prior (Stage 16 step 3).

    obs = net_prior.observer_until(session_steps...)   # see observer_for()
    search = mcts.Search(session, prior=net_prior.Prior(path), observer=obs)

Needs PyTorch: runs on duelpc (~/duelroom-ml), never imported on the server.
The prior is the softmax of the net's logits over the point's legal actions
(rows beyond the 128 encoded ones get 0, as ygorl's NetPolicy does).
"""

import math


class Prior:
    def __init__(self, checkpoint, device="cuda"):
        import torch
        from ygorl.train.checkpoint import cached_actor

        loaded = cached_actor(checkpoint)
        self.net, self.vocab, self.event_length = loaded.net, loaded.vocab, loaded.event_length
        self.device = torch.device(device if torch.cuda.is_available() else "cpu")
        self.net.to(self.device).eval()
        self.calls = 0

    def observer(self, cards=None):
        """A fresh PointObserver this net reads (feed it every point from the duel's start)."""
        from ygorl.engine.duel import default_cards
        from ygorl.env.observer import PointObserver

        return PointObserver(cards or default_cards(), self.vocab, self.event_length,
                             selection_history=self.net.cfg.selection_history,
                             attack_history=self.net.cfg.attack_history)

    def __call__(self, point, session, observer):
        import torch
        from ygorl.env.encoding import MAX_OPTIONS
        from ygorl.nets.batch import collate

        obs = observer.encode(point, session.core)
        with torch.no_grad():
            batch = collate([obs], self.device)
            logits = self.net(batch).logits[0].float().cpu().tolist()
        self.calls += 1
        n = len(point.actions)
        k = min(n, MAX_OPTIONS)
        xs = logits[:k]
        mask = obs["action_mask"][:k]
        top = max(x for x, m in zip(xs, mask) if m) if any(mask) else 0.0
        e = [math.exp(x - top) if m else 0.0 for x, m in zip(xs, mask)]
        z = sum(e) or 1.0
        return [v / z for v in e] + [0.0] * (n - k)


def observer_through(prior, session_factory, actions, cards=None):
    """(session, observer) after playing `actions` from a fresh session,
    the observer having seen every point -- the root of a search."""
    session = session_factory()
    obs = prior.observer(cards)
    for a in actions:
        p = session.point
        obs.observe(p)
        obs.on_decision(p, a)
        session.act(a)
    return session, obs
