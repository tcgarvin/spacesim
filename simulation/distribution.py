from random import normalvariate
from abc import ABC
from abc import abstractmethod

class Distribution(ABC):
    @abstractmethod
    def draw(self):
        pass

class Normal(Distribution):
    def __init__(self, mu, sigma):
        self.mu = mu
        self.sigma = sigma

    def draw(self):
        return normalvariate(self.mu, self.sigma)
