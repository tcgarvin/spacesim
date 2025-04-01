class Simulation(universe: Universe) : Tickable {
    val universe = universe
    override fun tick() {
        universe.tick()
    }
}