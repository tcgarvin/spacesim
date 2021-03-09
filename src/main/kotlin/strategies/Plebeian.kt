package strategies

import Person
import actions.MakeGood
import actions.PersonAction

class Plebeian : PersonStrategy {
    override fun pickNextAction(person: Person) : PersonAction  {
        return listOf(MakeGood.FOOD, MakeGood.WOOD).random();
    }
}