from person import Person

class PersonActor():
    def __init__(self, person : Person):
        self.person = person

    def tick(self, planet : 'Planet'):
        """
        Entrypoint for person logic
        """
        pass