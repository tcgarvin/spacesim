import kotlin.system.measureTimeMillis

fun main(args: Array<String>) {

    println("Starting Data Logger..")
    setLogger(SingleRandomDataLogger())

    println("Generating Universe..")
    val universeConfig = UniverseGenerationConfig(600.0, 100.0, 75, 60.0, 12.0, 90.0, 0.25, null)
    val universe = generateUniverse(universeConfig)

    println("Starting simulation..")
    val simulation = Simulation(universe)

    for (day in 1..100) {
        val tickDuration = measureTimeMillis {
            simulation.tick()
        }
        println("Day $day ( $tickDuration ms )")
    }

    val allPeople = universe.starSystems.flatMap { it.planets.flatMap { it.people }}
    val averageScore = allPeople.map { it.getScore() }.average()
    println("Average Score: $averageScore")
}