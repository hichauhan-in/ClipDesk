"""Show how ClipDesk would grade the models your provider actually offers.

    .\.venv\Scripts\python.exe tools\show_models.py
"""

from clipdesk.config import load_settings
from clipdesk.llm.budget import LEVELS, pick_model, rank_models
from clipdesk.llm.registry import build_provider

settings = load_settings()
status = build_provider(settings.llm).status()
models = list(status.models)

print(f"provider : {status.label}")
print(f"active   : {status.active_model}")
print(f"offered  : {len(models)}")
for name in models:
    print(f"  {name}")

print("\ngraded:")
for tier, names in rank_models(models).items():
    print(f"  {tier:<9}: {', '.join(names) or '(none)'}")

print("\nwhat each level would use:")
for budget in LEVELS:
    picks = {task: pick_model(models, budget.tier_for(task)) for task in ("analyse", "notes", "article", "clips")}
    print(f"  {budget.label:<15} " + "  ".join(f"{task}={name or 'default'}" for task, name in picks.items()))
