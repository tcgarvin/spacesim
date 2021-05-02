package markets

import kotlin.test.Test
import kotlin.test.assertFailsWith

class CommodityMarketTest {

    @Test
    internal fun testBuyAllAvailable() {
        val market = CommodityMarket()
        var unitsBought = 0
        market.issueBuyOrder(1,1) { _, numUnits, _ -> unitsBought += numUnits }
        market.issueSellOrder(1,1)

        market.run()

        assert(unitsBought == 1)
    }

    @Test
    internal fun testBuyLessThanAvailable() {
        val market = CommodityMarket()
        var unitsBought = 0
        market.issueBuyOrder(1,1) { _, numUnits, _ -> unitsBought += numUnits }
        market.issueSellOrder(2,1)

        market.run()

        assert(unitsBought == 1)
    }

    @Test
    internal fun testBuyMoreThanAvailable() {
        val market = CommodityMarket()
        var unitsBought = 0
        market.issueBuyOrder(2,1) { _, numUnits, _ -> unitsBought += numUnits }
        market.issueSellOrder(1,1)

        market.run()

        assert(unitsBought == 1)
    }

    @Test
    internal fun testNoMatch() {
        val market = CommodityMarket()
        var unitsBought = 0
        market.issueBuyOrder(1,1) { _, numUnits, _ -> unitsBought += numUnits }
        market.issueSellOrder(1,2)

        market.run()

        assert(unitsBought == 0)
    }

    @Test
    internal fun testFirstOrderFirst() {
        val market = CommodityMarket()
        var unitsBought = 0
        market.issueBuyOrder(1,1) { _, numUnits, _ -> unitsBought += numUnits }
        market.issueBuyOrder(1,1)
        market.issueSellOrder(1,1)

        market.run()

        assert(unitsBought == 1)
    }

    @Test
    internal fun testEmptyMarketRuns() {
        CommodityMarket().run()
    }
}