import ships.Ship
import ships.strategies.ShipStrategy
import ships.strategies.ShipStrategyOutput
import strategies.PersonStrategyOutput
import java.util.*
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

const val QUEUE_CAP = 10000

private var dataLoggerSingleton : DataLogger? = null;
fun getLogger() : DataLogger {
    return dataLoggerSingleton!!
}

fun setLogger(dataLogger : DataLogger) {
    dataLoggerSingleton = dataLogger
}

interface DataLogger {
    fun isFollowed(person: Person) : Boolean
    fun isFollowed(ship: Ship) : Boolean
    fun logPersonTurn(person: Person, actions: PersonStrategyOutput)
    fun logShipTurn(ship: Ship, actions: ShipStrategyOutput)
}

class NoDataLogger : DataLogger {
    override fun isFollowed(person: Person): Boolean = false
    override fun isFollowed(ship: Ship): Boolean = false
    override fun logPersonTurn(person: Person, actions: PersonStrategyOutput) = Unit
    override fun logShipTurn(ship: Ship, actions: ShipStrategyOutput) = Unit
}

class DataLogConsumer() : Runnable{
    val loggingQueue = LinkedBlockingQueue<String>(QUEUE_CAP)
    override fun run() {
        while (true) {
            val message = loggingQueue.take()
            println(message)
        }
    }
}

class RandomSingleDataLogger : DataLogger {
    private val followedPersons = mutableListOf<Person>()
    private val followedShips = mutableListOf<Ship>()
    private val followLock = ReentrantLock()
    private val logConsumer = DataLogConsumer()
    private val consumer = Thread(logConsumer)

    init {
        consumer.isDaemon = true
        consumer.start()
    }

    override fun isFollowed(person:Person) : Boolean {
        // We do want to find a person if we're not following anyone today.  Not very robust
        if (followedPersons.size == 0) {
            followLock.withLock {
                if (Math.random() > 0.999) {
                    println("Following person ${person.id}")
                    followedPersons.add(person)
                }
            }
        }

        return followedPersons.contains(person)
    }

    override fun isFollowed(ship:Ship) : Boolean {
        if (followedShips.size == 0) {
            followLock.withLock {
                if (Math.random() > 0.9) {
                    println("Following ship ${ship.id}")
                    followedShips.add(ship)
                }
            }
        }

        return followedShips.contains(ship)
    }

    override fun logPersonTurn(person: Person, actions: PersonStrategyOutput) {
        isFollowed(person) || return

        var logLine = "Person ${person.id.toString().slice(0 until 8)}: {"
        for ((good, count) in person.goods) {
            logLine += "${good.name}:$count, "
        }

        logLine += "money: ${person.money}, score: ${person.needs.getScore(person)}} ${actions.personAction}. "

        for (action in actions.marketActions) {
            logLine += "${action}, "
        }
        logConsumer.loggingQueue.add(logLine)
    }

    override fun logShipTurn(ship: Ship, actions: ShipStrategyOutput) {
        isFollowed(ship) || return

        var logLine = "Ship ${shortUUID(ship.id)}: {"
        for ((good, count) in ship.goods) {
            logLine += "${good.name}:$count, "
        }

        logLine += "money: ${ship.money}} ${actions.shipAction}. "

        for (action in actions.marketActions) {
            logLine += "${action}, "
        }
        logConsumer.loggingQueue.add(logLine)
    }
}

fun shortUUID(uuid: UUID) : String {
    return uuid.toString().slice(0 until 8)
}