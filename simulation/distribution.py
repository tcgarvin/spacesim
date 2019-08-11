from random import normalvariate


class Normal:
    def __init__(self, mu, sigma):
        self.mu = mu
        self.sigma = sigma

    def draw(self):
        return normalvariate(self.mu, self.sigma)
