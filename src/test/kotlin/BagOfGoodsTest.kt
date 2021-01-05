import kotlin.test.Test
import kotlin.test.assertFailsWith

class BagOfGoodsTest {
    @Test
    internal fun testNoNegativeGoods() {
        val bag = BagOfGoods()
        assertFailsWith<BagOfGoods.ValueException> { bag[GoodKind.FOOD] -= 1 }
    }
}