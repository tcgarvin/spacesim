package actions

import Person

interface PersonAction {
    /**
     * Apply this action to the given person.
     */
    fun apply(person: Person)
}

interface MarketAction {
    /**
     * Apply this action to the given person.
     */
    fun apply(person: Person)

    /**
     * Cancel this action
     */
    fun cancel(person: Person)
}

object NoOpMarketAction : MarketAction {
    override fun apply(person: Person) {}

    override fun cancel(person: Person) {}
}