package actions

import Person

object WorkForGovernment : PersonAction {
    override fun apply(person: Person) {
        person.addMoney(10)
    }
}
