class BagOfGoods private constructor(private val goodCounts: MutableMap<GoodKind, Int>) :
    MutableMap<GoodKind, Int> by goodCounts {

    // This pattern is something like a public constructor, so we can contain our dependence on a fresh hashmap
    companion object {
        operator fun invoke(): BagOfGoods {
            return BagOfGoods(HashMap())
        }
    }

    /**
     * Returns a new bag of goods that is a quantitative multiple of this one
     */
    fun several(multiple: Int): BagOfGoods {
        val result = BagOfGoods()
        goodCounts.mapValuesTo(result.goodCounts, { it.value * multiple })
        return result
    }

    /**
     * Divides one bag of goods by another.  Returns the quotient as an integer.  (Whole number quotients, only.
     * "Natural" division, no negatives, floats, etc.)
     */
    fun divide(other: BagOfGoods): Int {
        if (other.isEmpty()) {
            throw ArithmeticException("Division by Zero")
        }

        val quotient: Int? = goodCounts
            .map { (kind, number) -> number / other[kind] }
            .minOrNull()

        return quotient ?: 0
    }

    fun divideWithRemainder(other: BagOfGoods): Pair<Int, BagOfGoods> {
        val quotient = divide(other)
        val remainder = BagOfGoods()
        goodCounts.mapValuesTo(remainder.goodCounts, { (kind, value) -> value - other[kind] * quotient })
        return Pair(quotient, remainder)
    }

    override operator fun get(key: GoodKind): Int {
        return goodCounts.getOrDefault(key, 0)
    }

    operator fun set(kind: GoodKind, number: Int) {
        if (number < 0) {
            throw ValueException("Cannot have less than zero of a good")
        }

        if (number == 0) {
            goodCounts.remove(kind)
        }
        else {
            goodCounts[kind] = number
        }
    }

    class ValueException(message: String) : Exception(message)
}