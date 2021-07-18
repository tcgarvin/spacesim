import burlap.mdp.singleagent.SADomain
import kotlin.system.measureTimeMillis

fun main(args: Array<String>) {

    println("Starting Data Logger..")
    setLogger(NoDataLogger())

    val domain = SADomain()
    domain.addActionType()

    println("Generating Universe..")
    val universe = generateUniverse()

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