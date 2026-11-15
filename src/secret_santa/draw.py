"""The draw itself, kept free of I/O so it is easy to test."""

import random
import secrets
from collections.abc import Hashable, Sequence

MIN_PARTICIPANTS = 3


def draw[T: Hashable](people: Sequence[T], rng: random.Random | None = None) -> dict[T, T]:
    """Return {giver: receiver}.

    Shuffles everyone into one random circle and has each person give to the next one. Nobody
    draws themselves, and with 3+ people there are no pairs who simply swap gifts (which would
    let each of them work out the other's assignment).
    """
    if len(set(people)) != len(people):
        raise ValueError("participants must be unique")
    if len(people) < MIN_PARTICIPANTS:
        raise ValueError(f"need at least {MIN_PARTICIPANTS} participants")

    order = list(people)
    (rng or secrets.SystemRandom()).shuffle(order)
    return {giver: order[(i + 1) % len(order)] for i, giver in enumerate(order)}
