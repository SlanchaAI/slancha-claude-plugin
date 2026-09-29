---
description: Install or update the skills your Slancha library publishes
argument-hint: "[skill] [--dry-run] [--force]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/slancha.py" *)
disable-model-invocation: true
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/slancha.py" update --project "${CLAUDE_PROJECT_DIR}" $ARGUMENTS`

The Slancha update above has already run. Show its report to the user exactly as printed, in a code block, and add nothing else. Do not run any tools.
