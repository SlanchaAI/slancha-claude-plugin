---
description: Show the Slancha token, API and installed Slancha skills
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/slancha.py" *)
disable-model-invocation: true
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/slancha.py" status --project "${CLAUDE_PROJECT_DIR}"`

The Slancha status above has already been collected. Show it to the user exactly as printed, in a code block, and add nothing else. Do not run any tools.
