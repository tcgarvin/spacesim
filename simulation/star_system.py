import math

class StarSystem:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y
        self.neighbors = []

    def __str__(self):
        return '{ x: ' + str(self.x) + ', y:' + str(self.y) + ', ' + str(self.neighbors) + '}'

    def distanceBetween(self, other):
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)

    def add_neighbor(self, neighbor):
        self.neighbors.append(neighbor)