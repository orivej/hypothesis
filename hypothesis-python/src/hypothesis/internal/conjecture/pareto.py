# coding=utf-8
#
# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis/
#
# Most of this work is copyright (C) 2013-2019 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# CONTRIBUTING.rst for a full list of people who may hold copyright, and
# consult the git log if you need to determine who owns an individual
# contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.
#
# END HEADER

from __future__ import absolute_import, division, print_function

from enum import Enum

from sortedcontainers import SortedList

from hypothesis.internal.conjecture.data import ConjectureData, ConjectureResult, Status
from hypothesis.internal.conjecture.junkdrawer import LazySequenceCopy, swap
from hypothesis.internal.conjecture.shrinker import sort_key

NO_SCORE = float("-inf")


class DominanceRelation(Enum):
    NO_DOMINANCE = 0
    EQUAL = 1
    LEFT_DOMINATES = 2
    RIGHT_DOMINATES = 3


def dominance(left, right):
    """Returns the dominance relation between ``left`` and ``right``, according
    to the rules that one ConjectureResult dominates another if and only if it
    is better in every way.

    The things we currently consider to be "better" are:

        * Something that is smaller in shrinking order is better.
        * Something that has higher status is better.
        * Each ``interesting_origin`` is treated as its own score, so if two
          interesting examples have different origins then neither dominates
          the other.
        * For each target observation, a higher score is better.

    In "normal" operation where there are no bugs or target observations, the
    pareto front only has one element (the smallest valid test case), but for
    more structured or failing tests it can be useful to track, and future work
    will depend on it more."""

    if left.buffer == right.buffer:
        return DominanceRelation.EQUAL

    if sort_key(right.buffer) < sort_key(left.buffer):
        result = dominance(right, left)
        if result == DominanceRelation.LEFT_DOMINATES:
            return DominanceRelation.RIGHT_DOMINATES
        else:
            # Because we have sort_key(left) < sort_key(right) the only options
            # are that right is better than left or that the two are
            # incomparable.
            assert result == DominanceRelation.NO_DOMINANCE
            return result

    # Either left is better or there is no dominance relationship.
    assert sort_key(left.buffer) < sort_key(right.buffer)

    # The right is more interesting
    if left.status < right.status:
        return DominanceRelation.NO_DOMINANCE

    if not right.tags.issubset(left.tags):
        return DominanceRelation.NO_DOMINANCE

    # Things that are interesting for different reasons are incomparable in
    # the dominance relationship.
    if (
        left.status == Status.INTERESTING
        and left.interesting_origin != right.interesting_origin
    ):
        return DominanceRelation.NO_DOMINANCE

    for target in set(left.target_observations) | set(right.target_observations):
        left_score = left.target_observations.get(target, NO_SCORE)
        right_score = right.target_observations.get(target, NO_SCORE)
        if right_score > left_score:
            return DominanceRelation.NO_DOMINANCE

    return DominanceRelation.LEFT_DOMINATES


class ParetoFront(object):
    """Maintains an approximate pareto front of ConjectureData objects. That
    is, we try to maintain a collection of objects such that no element of the
    collection is pareto dominated by any other. In practice we don't quite
    manage that, because doing so is computationally very expensive. Instead
    we maintain a random sample of data objects that are "rarely" dominated by
    any other element of the collection (roughly, no more than about 10%).

    Only valid test cases are considered to belong to the pareto front - any
    test case with a status less than valid is discarded.

    Note that the pareto front is potentially quite large, and currently this
    will store the entire front in memory. This is bounded by the number of
    valid examples we run, which is max_examples in normal execution, and
    currently we do not support workflows with large max_examples which have
    large values of max_examples very well anyway, so this isn't a major issue.
    In future we may weish to implement some sort of paging out to disk so that
    we can work with larger fronts.

    Additionally, because this is only an approximate pareto front, there are
    scenarios where it can be much larger than the actual pareto front. There
    isn't a huge amount we can do about this - checking an exact pareto front
    is intrinsically quadratic.

    "Most" of the time we should be relatively close to the true pareto front,
    say within an order of magnitude, but it's not hard to construct scenarios
    where this is not the case. e.g. suppose we enumerate all valid test cases
    in increasing shortlex order as s_1, ..., s_n, ... and have scores f and
    g such that f(s_i) = min(i, N) and g(s_i) = 1 if i >= N, then the pareto
    front is the set {s_1, ..., S_N}, but the only element of the front that
    will dominate s_i when i > N is S_N, which we select with probability
    1 / N. A better data structure could solve this, but at the cost of more
    expensive operations and higher per element memory use, so we'll wait to
    see how much of a problem this is in practice before we try that.
    """

    def __init__(self, random):
        self.__random = random
        self.__eviction_listeners = []

        self.__front = SortedList(key=lambda d: sort_key(d.buffer))
        self.__pending = None

    def add(self, data):
        """Attempts to add ``data`` to the pareto front. Returns True if
        ``data`` is now in the front, including if data is already in the
        collection, and False otherwise"""
        data = data.as_result()
        if data.status < Status.VALID:
            return False

        if not self.__front:
            self.__front.add(data)
            return True

        if data in self.__front:
            return True

        # We add data to the pareto front by adding it unconditionally and then
        # doing a certain amount of randomized "clear down" - testing a random
        # set of elements (currently 10) to see if they are dominated by
        # something else in the collection. If they are, we remove them.
        self.__front.add(data)
        assert self.__pending is None
        try:
            self.__pending = data

            # We maintain a set of the current exact pareto front of the
            # values we've sampled so far. When we sample a new element we
            # either add it to this exact pareto front or remove it from the
            # collection entirely.
            front = LazySequenceCopy(self.__front)

            # We track which values we are going to remove and remove them all
            # at the end so the shape of the front doesn't change while we're
            # using it.
            to_remove = []

            # We now iteratively sample elements from the approximate pareto
            # front to check whether they should be retained. When the set of
            # dominators gets too large we have sampled at least 10 elements
            # and it gets too expensive to continue, so we consider that enough
            # due diligence.
            i = self.__front.index(data)

            # First we attempt to look for values that must be removed by the
            # addition of the data. These are necessarily to the right of it
            # in the list.

            failures = 0
            while i + 1 < len(front) and failures < 10:
                j = self.__random.randrange(i + 1, len(front))
                swap(front, j, len(front) - 1)
                candidate = front.pop()
                dom = dominance(data, candidate)
                assert dom != DominanceRelation.RIGHT_DOMINATES
                if dom == DominanceRelation.LEFT_DOMINATES:
                    to_remove.append(candidate)
                    failures = 0
                else:
                    failures += 1

            # Now we look at the points up to where we put data in to see if
            # it is dominated. While we're here we spend some time looking for
            # anything else that might be dominated too, compacting down parts
            # of the list.

            dominators = [data]

            while i >= 0 and len(dominators) < 10:
                swap(front, i, self.__random.randint(0, i))

                candidate = front[i]

                already_replaced = False
                j = 0
                while j < len(dominators):
                    v = dominators[j]

                    dom = dominance(candidate, v)
                    if dom == DominanceRelation.LEFT_DOMINATES:
                        if not already_replaced:
                            already_replaced = True
                            dominators[j] = candidate
                            j += 1
                        else:
                            dominators[j], dominators[-1] = (
                                dominators[-1],
                                dominators[j],
                            )
                            dominators.pop()
                        to_remove.append(v)
                    elif dom == DominanceRelation.RIGHT_DOMINATES:
                        to_remove.append(candidate)
                        break
                    elif dom == DominanceRelation.EQUAL:
                        break
                    else:
                        j += 1
                else:
                    dominators.append(candidate)
                i -= 1

            for v in to_remove:
                self.__remove(v)
            return data in self.__front
        finally:
            self.__pending = None

    def on_evict(self, f):
        """Register a listener function that will be called with data when it
        gets removed from the front because something else dominates it."""
        self.__eviction_listeners.append(f)

    def __contains__(self, data):
        return isinstance(data, (ConjectureData, ConjectureResult)) and (
            data.as_result() in self.__front
        )

    def __iter__(self):
        return iter(self.__front)

    def __getitem__(self, i):
        return self.__front[i]

    def __len__(self):
        return len(self.__front)

    def __remove(self, data):
        try:
            self.__front.remove(data)
        except ValueError:
            return
        if data is not self.__pending:
            for f in self.__eviction_listeners:
                f(data)