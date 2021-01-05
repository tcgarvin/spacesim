fun main(args: Array<String>) {

    println("Generating Universe")
    val universe = generateUniverse()

    println("Starting simulation")
    val simulation = Simulation(universe)

    for (day in 1..100) {
        println("Day $day")
        simulation.tick()
    }

    val allPeople = universe.starSystems.flatMap { it.planets.flatMap { it.people }}
    val averageScore = allPeople.map { it.getScore() }.average()
    println("Average Score: $averageScore")
}