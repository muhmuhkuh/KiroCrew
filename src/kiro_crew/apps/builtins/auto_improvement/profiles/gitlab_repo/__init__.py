"""GitLab target profile package — MR drafting for the auto-improvement loop."""

from .pr_recipe import GitLabPRRecipe, extract_mr_url

__all__ = ["GitLabPRRecipe", "extract_mr_url"]
