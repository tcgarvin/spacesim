"""Discovery-phase probing of the market maker.

The two probes walk toward each other: the bid doubles up from the lower
bound, the ask halves down toward it. The posted bid must stay under our own
ask, but the bid *ladder* must not be pinned by that clamp, or a maker
holding inventory can never escalate its bid again.
"""

from types import SimpleNamespace

from spacesim2.core.brains.market_maker_2 import MarketMakerBrain, MarketMakerState
from spacesim2.core.commands import PlaceBuyOrderCommand, PlaceSellOrderCommand
from spacesim2.core.commodity import CommodityDefinition

_WIDGET = CommodityDefinition(
    id="widget", name="Widget", transportable=True, description="A test good."
)


def _stocked_actor(quantity: int = 5) -> SimpleNamespace:
    """An actor stand-in that only has to answer inventory questions."""
    return SimpleNamespace(
        inventory=SimpleNamespace(get_quantity=lambda _commodity: quantity)
    )


def _probe_prices(commands):
    """The (bid, ask) prices in one turn's discovery commands."""
    bid = next(
        (c.price for c in commands if isinstance(c, PlaceBuyOrderCommand)),
        None,
    )
    ask = next(
        (c.price for c in commands if isinstance(c, PlaceSellOrderCommand)),
        None,
    )
    return bid, ask


def test_discovery_bid_ladder_escalates_without_crossing_our_own_ask():
    """The stored rung keeps doubling even while the posted bid is clamped."""
    brain = MarketMakerBrain()
    actor = _stocked_actor()
    state = MarketMakerState(lower_bound=1, upper_bound=64)

    ladder = []
    posted = []
    for _turn in range(6):
        bid, ask = _probe_prices(
            brain._discovery_quotes(actor, _WIDGET, state, per_market_budget=1000.0)
        )
        assert ask is not None
        assert bid is not None
        # Never fill our own ask.
        assert bid < ask
        ladder.append(state.last_bid_quote)
        posted.append(bid)

    # The ladder escalates every turn; without that the maker sat at the
    # lower bound forever for any commodity it held inventory in.
    assert ladder == sorted(ladder)
    assert ladder[-1] > ladder[0]
    assert all(later > earlier for earlier, later in zip(ladder, ladder[1:]))
    # The posted bid is only ever the ladder rung or the clamp under our ask.
    assert all(p <= rung for p, rung in zip(posted, ladder))


def test_discovery_bid_is_unclamped_without_inventory():
    """With nothing to sell there is no ask to stay under."""
    brain = MarketMakerBrain()
    actor = _stocked_actor(quantity=0)
    state = MarketMakerState(lower_bound=1, upper_bound=64)

    brain._discovery_quotes(actor, _WIDGET, state, per_market_budget=1000.0)
    bid, ask = _probe_prices(
        brain._discovery_quotes(actor, _WIDGET, state, per_market_budget=1000.0)
    )
    assert ask is None
    assert bid == state.last_bid_quote == 2
