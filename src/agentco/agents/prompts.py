from importlib import resources

from agentco.roles import Role


def load_prompt(role: Role, variant: str | None = None) -> str:
    name = Role(role).value + (f"-{variant}" if variant else "")
    return (
        resources.files("agentco.agents")
        .joinpath("prompts", f"{name}.md")
        .read_text(encoding="utf-8")
    )
