import altair as alt
from collections import defaultdict
import json
import numpy as np
import pandas as pd
import streamlit as st
import time

from good import food, wood
from log import SimulationLogger, make_log_dir_name
from model_json import universe_to_json
from person_need import FoodNeed, ShelterNeed
from universe import Universe

MAX_TICKS = 1000

st.title("SpaceSim")
st.markdown("This is a toy economic simulation situated in a toy galaxy.")

status_placeholder = st.empty()

status_placeholder.text("Initializing")
universe = Universe()

st.header("Goods Distributions")
goods_dist_chart = st.empty()

tick = 0
#logger = SimulationLogger(make_log_dir_name())
while tick <= MAX_TICKS:
    status_placeholder.text(f"Day {tick}")
    universe.tick()

    if tick % 2 == 0:
        #good_amounts = pd.DataFrame(0, index=np.arange(41), columns=(food.name, wood.name))
        #for system in universe.systems:
        #    for planet in system.planets:
        #        for person in planet.people.values():
        #            for kind, amount in person.goods.items():
        #                bounded_amount = min(amount, 40)
        #                good_amounts[kind.name].loc[bounded_amount] += 1

        good_amounts = {kind.name: [0] * 41 for kind in (food, wood)}
        need_scores = {
            need.__name__: [0] * (need.MAX_FULFILLMENT_SCORE + 1)
            for need in (FoodNeed, ShelterNeed)
        }
        for system in universe.systems:
            for planet in system.planets:
                for person in planet.people.values():
                    for kind, amount in person.goods.items():
                        bounded_amount = min(amount, 40)
                        good_amounts[kind.name][bounded_amount] += 1

        #food_chart = alt.Chart(good_amounts.reset_index()).mark_bar().encode(
        goods_chart = alt.Chart(pd.DataFrame(good_amounts).reset_index()).mark_bar().encode(
            alt.X("index:Q"),
            alt.Y(alt.repeat("row"), type="quantitative")
        ).properties(
            height=150
        ).repeat(
            row=[food.name, wood.name]
        )
        goods_dist_chart.altair_chart(goods_chart)

    tick += 1
    #klogger.log_state(tick, universe_to_json(self.universe))

status_placeholder.text(f"Done after {tick} days")