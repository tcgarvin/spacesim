package needs

import Person

abstract class Need {

    /**
     * Attempts to meet the need for the person.  May consume goods or have other effects on the person.
     */
    abstract fun visit(person: Person)

    /**
     * Returns the current score
     */
    abstract fun getScore(person: Person): Double
}

abstract class PhysicalNeed : Need() {
    /**
     * A need in the spirit of Mavlov
     */
}

