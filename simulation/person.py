from good import BagOfGoods
class Person:
    """
    A planet-bound person.
    """
    def __init__(self):
        self.goods = BagOfGoods()
        self.money = 0
        self.factories = []

def generate_person():
    return Person()
