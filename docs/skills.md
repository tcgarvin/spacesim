# Skills

Actors hold general skills that apply across related processes. Skill
definitions live in `data/skills.yaml` (`core/skill.py` loads them); each
process lists its `relevant_skills` in `data/processes.yaml`.

| Skill | Covers |
|-------|--------|
| `mining` | Extracting ores and minerals |
| `refining` | Smelting metals, refining fuels |
| `simple_manufacturing` | Simple tools, building materials |
| `advanced_manufacturing` | Complex facilities, high-tech tools |
| `agriculture` | Growing biomass, making food |
| `culinary` | Specialized food and consumables |
| `aerospace` | Fuel refinement, starship maintenance, spacecraft |
| `chemistry` | Chemicals, pharmaceuticals, advanced fuels |

Example mappings: `mine_common_metal_ore` uses `mining`; `refine_nova_fuel`
uses `chemistry` and `refining`; `make_food` uses `culinary` and
`agriculture`. A process with several skills uses their average rating
(`SkillCheck.get_combined_skill_rating`).

## Ratings

Ratings range from 0.5 (unskilled) to 3.0, clamped in
`Actor.set_skill_rating`. An actor with no rating for a skill counts as 0.5.
`ProcessCommand.execute` (`core/commands.py`) applies two checks:

- **Success** (`SkillCheck.success_check`): rating >= 1.0 always succeeds.
  Below 1.0 the success probability equals the rating, so 0.8 succeeds 80%
  of the time. A failed process consumes nothing and produces nothing.
- **Multiplier** (`SkillCheck.multiplier_check`): chance is
  `(rating - 1.0) * 0.5`, so 0% at 1.0, 25% at 1.5, 50% at 2.0, 100% at
  3.0. A multiplier doubles both inputs consumed and outputs produced.

Each successful run improves every relevant skill by 0.01, or 0.03 when the
multiplier fired.

## Example

Actor with `mining: 2.0`, `refining: 1.2`, `simple_manufacturing: 0.8`:

| Process | Success | Multiplier chance | Outcomes |
|---------|---------|-------------------|----------|
| Mine ore (mining 2.0) | 100% | 50% | half the runs produce double output from double input |
| Refine metal (refining 1.2) | 100% | 10% | one run in ten doubles |
| Make simple tools (manufacturing 0.8) | 80% | 0% | one run in five fails and consumes nothing |

Adding a process only requires linking it to existing skills, and actors
specialize over time through the per-run improvement.
