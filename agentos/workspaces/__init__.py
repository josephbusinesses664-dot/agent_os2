"""Isolated engineering workspaces.

Each coding task can run in its own git worktree (or plain sandbox dir), so
many agents work in parallel without clobbering each other, then integrate()
merges the work back through review → tests → merge. See manager.py (git
mechanics) and harness.py (the coding-runtime interface).
"""