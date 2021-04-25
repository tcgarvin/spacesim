package generation

import org.apache.commons.math3.distribution.NormalDistribution
import org.jgrapht.alg.connectivity.ConnectivityInspector
import org.jgrapht.graph.DefaultEdge
import org.jgrapht.graph.SimpleGraph
import org.tinfour.common.IQuadEdge
import org.tinfour.common.Vertex
import org.tinfour.standard.IncrementalTin
import kotlin.random.Random

/**
 * This generator uses the Tinfour project and corresponding data structures to generate a map of the galaxy.  Rather
 * than invent our own intermediary interface for outside usage, we require client classes to use the same data
 * structures, though we expect that those client classes will turn them into game objects.
 */
class StarSystemMapGenerator() {
    var starLocations = listOf<Vertex>()
        private set

    var starLanes = listOf<IQuadEdge>()
        private set

    private fun generatePoints(
        mapSideLength: Double,
        sigma: Double,
        numStars: Int,
        coreRadius: Double,
        starMargin: Double
    ): List<Vertex> {
        val center = mapSideLength / 2
        val origin = Vertex(center, center, 0.0)
        val starDistribution = NormalDistribution(center, sigma)

        val result = mutableListOf<Vertex>()
        while (result.size < numStars) {
            val candidate = Vertex(starDistribution.sample(), starDistribution.sample(), 0.0)
            if (candidate.getDistance(origin) < coreRadius) {
                continue
            }

            if (result.any { candidate.getDistance(it) < starMargin }) {
                continue
            }

            result.add(candidate)
        }

        return result
    }

    private fun generateEdges(
        points: List<Vertex>,
        maxLaneLength: Double,
        percentEdgesToRemove: Double
    ): List<IQuadEdge> {
        val tin = IncrementalTin()
        tin.add(points, null)
        return tin.edges.filter { it.length < maxLaneLength }.filter { Random.nextDouble() > percentEdgesToRemove }
    }

    /**
     * Removes all points (and their corresponding edges) that do not exist in the largest component (in the graph
     * theory sense).  The resulting points and edges should be only those in the large primary component.
     */
    private fun trimExtraneous(points : List<Vertex>, edges: List<IQuadEdge>) : Pair<List<Vertex>, List<IQuadEdge>> {
        val graph = SimpleGraph<Vertex, DefaultEdge>(DefaultEdge::class.java)

        for (point in points) {
            graph.addVertex(point)
        }

        for (edge in edges) {
            graph.addEdge(edge.a, edge.b)
        }

        val inspector = ConnectivityInspector(graph)
        val components = inspector.connectedSets().sortedByDescending { it.size }
        val componentsToRemove = components.drop(1)
        val pointsToRemove = componentsToRemove.reduce { a,b -> a.addAll(b); a }

        val trimmedPoints = points.filterNot { pointsToRemove.contains(it) }
        val trimmedEdges = edges.filterNot { pointsToRemove.contains(it.a) }
        return Pair(trimmedPoints, trimmedEdges)
    }

    fun generate(
        mapSideLength: Double,
        sigma: Double,
        numStars: Int,
        coreRadius: Double,
        starMargin: Double,
        maxLaneLength: Double,
        percentEdgesToRemove: Double
    ) {
        val originalStarLocations = generatePoints(mapSideLength, sigma, numStars, coreRadius, starMargin)
        val originalStarLanes = generateEdges(starLocations, maxLaneLength, percentEdgesToRemove)
        val (trimmedStarLocations, trimmedStarLanes) = trimExtraneous(originalStarLocations, originalStarLanes)

        starLocations = trimmedStarLocations
        starLanes = trimmedStarLanes
    }
}
