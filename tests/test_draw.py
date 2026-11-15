import random

import pytest

from secret_santa.draw import draw


@pytest.mark.parametrize("n", [3, 4, 5, 10, 31])
def test_everyone_gives_and_receives_once_never_to_self(n: int) -> None:
    people = list(range(n))
    for seed in range(200):
        pairs = draw(people, random.Random(seed))
        assert sorted(pairs) == people
        assert sorted(pairs.values()) == people
        assert all(giver != receiver for giver, receiver in pairs.items())


def test_is_one_cycle_so_nobody_just_swaps() -> None:
    people = list("ABCDEFG")
    for seed in range(200):
        pairs = draw(people, random.Random(seed))
        seen, current = set(), people[0]
        while current not in seen:
            seen.add(current)
            current = pairs[current]
        assert seen == set(people)


def test_all_orders_possible() -> None:
    # 4 people -> (4-1)! = 6 distinct circles; a fair shuffle should hit all of them.
    results = {tuple(sorted(draw("ABCD", random.Random(s)).items())) for s in range(500)}
    assert len(results) == 6


@pytest.mark.parametrize("people", [[], [1], [1, 2]])
def test_too_few(people: list[int]) -> None:
    with pytest.raises(ValueError, match="at least"):
        draw(people)


def test_duplicates_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        draw([1, 2, 2, 3])
