package strategies

import Person
import actions.MakeGood
import actions.PersonAction

class Plebian : PersonStrategy {
    override fun pickNextAction(person: Person) : PersonAction  {
        return MakeGood.FOOD
    }
}