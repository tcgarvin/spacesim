import random
from pathlib import Path
from typing import Dict, List

import yaml


class Skill:
    """A skill that actors hold at varying levels."""

    def __init__(self, id: str, name: str, description: str):
        """Initialize a skill definition."""
        self.id = id
        self.name = name
        self.description = description

    def __str__(self) -> str:
        return self.name


class SkillsRegistry:
    """Registry for skills in the simulation."""

    def __init__(self) -> None:
        """Initialize an empty skills registry."""
        self._skills: Dict[str, Skill] = {}

    def register_skill(self, skill: Skill) -> None:
        """Register a skill."""
        self._skills[skill.id] = skill

    def load_from_file(self, filepath: str | Path) -> None:
        """Load skills from a YAML file."""
        try:
            with open(filepath, "r") as f:
                skills_data = yaml.safe_load(f)

            for skill_data in skills_data:
                skill = Skill(
                    id=skill_data["id"],
                    name=skill_data["name"],
                    description=skill_data["description"],
                )
                self.register_skill(skill)
        except Exception as e:
            print(f"Error loading skills from {filepath}: {e}")


class SkillCheck:
    """Skill check rolls."""

    @staticmethod
    def success_check(skill_rating: float) -> bool:
        """Roll a success check.

        A rating of 1.0 or more always succeeds; below that the success
        probability equals the rating.
        """
        if skill_rating >= 1.0:
            return True

        return random.random() < skill_rating

    @staticmethod
    def multiplier_check(skill_rating: float) -> bool:
        """Roll for an output multiplier.

        Ratings at or below 1.0 never get one; above that the chance is half
        the excess over 1.0.
        """
        if skill_rating <= 1.0:
            return False

        multiplier_chance = (skill_rating - 1.0) * 0.5
        return random.random() < multiplier_chance

    @staticmethod
    def get_combined_skill_rating(skill_ratings: List[float]) -> float:
        """Average the given skill ratings; 0.5 (unskilled) if none."""
        if not skill_ratings:
            return 0.5

        return sum(skill_ratings) / len(skill_ratings)
