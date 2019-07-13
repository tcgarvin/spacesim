from universe import Universe

class Simulation:
    def __init__(self, universe: Universe):
        self.universe = universe
        self.max_ticks = 100

    def run(self):
        tick = 0
        while(tick < self.max_ticks):
            print(tick)
            self.universe.tick()
            tick += 1