package actions

import GoodKind
import Person

enum class MakeGood(val kind: GoodKind) : PersonAction {
    FOOD(GoodKind.FOOD),
    WOOD(GoodKind.WOOD);

    override fun apply(person: Person) {
        person.goods[kind] += 1
    }
}