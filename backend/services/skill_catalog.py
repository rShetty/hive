"""Skill catalog management and seeding."""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.skill import Skill


# Initial set of core and connected skills
DEFAULT_SKILLS = [
    # Core skills - no auth required
    {
        "name": "terminal",
        "display_name": "Terminal Access",
        "description": "Execute shell commands and manage files",
        "tier": "core",
        "category": "system",
        "required_env_vars": [],
        "definition": {
            "kind": "prompt",
            "instructions": "You have terminal access. You can execute shell commands to manage files, install packages, run scripts, and perform system operations. When the user asks you to run a command, do it directly.",
        },
    },
    {
        "name": "web_extract",
        "display_name": "Web Extraction",
        "description": "Fetch and parse content from URLs",
        "tier": "core",
        "category": "web",
        "required_env_vars": [],
        "definition": {
            "kind": "prompt",
            "instructions": "You can fetch and extract content from web pages. When the user provides a URL or asks you to look up something online, use your web extraction capability to retrieve and summarize the content.",
        },
    },
    {
        "name": "file_ops",
        "display_name": "File Operations",
        "description": "Read, write, and manage local files",
        "tier": "core",
        "category": "system",
        "required_env_vars": [],
        "definition": {
            "kind": "prompt",
            "instructions": "You can read, write, and manage local files. When the user asks you to create, modify, or inspect files, do it directly using your file operation capabilities.",
        },
    },
    {
        "name": "planning",
        "display_name": "Task Planning",
        "description": "Break down complex tasks into implementation plans",
        "tier": "core",
        "category": "productivity",
        "required_env_vars": [],
        "definition": {
            "kind": "prompt",
            "instructions": "You are skilled at breaking down complex tasks into clear, actionable implementation plans. When faced with a large task, first create a step-by-step plan, then execute each step systematically.",
        },
    },
    {
        "name": "code_review",
        "display_name": "Code Review",
        "description": "Review code changes and provide feedback",
        "tier": "core",
        "category": "development",
        "required_env_vars": [],
        "definition": {
            "kind": "prompt",
            "instructions": "You are an expert code reviewer. When reviewing code, look for bugs, security issues, performance problems, and style violations. Provide constructive feedback with specific suggestions for improvement.",
        },
    },
    {
        "name": "arxiv",
        "display_name": "arXiv Research",
        "description": "Search and retrieve academic papers from arXiv",
        "tier": "core",
        "category": "research",
        "required_env_vars": [],
        "definition": {
            "kind": "prompt",
            "instructions": "You can search and retrieve academic papers from arXiv. When the user asks about research papers or academic topics, search arXiv and summarize relevant findings.",
        },
    },
    
    # Connected skills - require user auth
    {
        "name": "github_pr",
        "display_name": "GitHub PR Workflow",
        "description": "Create and manage GitHub pull requests",
        "tier": "connected",
        "category": "development",
        "required_env_vars": ["GITHUB_TOKEN"],
        "definition": {
            "kind": "prompt",
            "instructions": "You can create and manage GitHub pull requests. When the user asks you to create a PR, review one, or manage GitHub workflows, use the GitHub API via the available MCP tools.",
        },
    },
    {
        "name": "linear",
        "display_name": "Linear Issues",
        "description": "Manage Linear project issues",
        "tier": "connected",
        "category": "productivity",
        "required_env_vars": ["LINEAR_API_KEY"],
        "definition": {
            "kind": "prompt",
            "instructions": "You can manage Linear project issues. When the user asks about creating, updating, or tracking issues, use the Linear API via the available MCP tools.",
        },
    },
    {
        "name": "obsidian",
        "display_name": "Obsidian Notes",
        "description": "Read and write to Obsidian vault",
        "tier": "connected",
        "category": "productivity",
        "required_env_vars": ["OBSIDIAN_VAULT_PATH"],
        "definition": {
            "kind": "prompt",
            "instructions": "You can read and write to an Obsidian vault. When the user asks you to take notes, search their knowledge base, or organize information, use your Obsidian integration.",
        },
    },
    {
        "name": "notion",
        "display_name": "Notion Integration",
        "description": "Read and write Notion pages and databases",
        "tier": "connected",
        "category": "productivity",
        "required_env_vars": ["NOTION_TOKEN"],
        "definition": {
            "kind": "prompt",
            "instructions": "You can read and write Notion pages and databases. When the user asks about Notion content, use the Notion API via the available MCP tools.",
        },
    },
    {
        "name": "openclaw",
        "display_name": "OpenClaw VPS Deploy",
        "description": "Deploy and manage OpenClaw instances on a VPS via Docker",
        "tier": "connected",
        "category": "deployment",
        "required_env_vars": [],
        "definition": {
            "kind": "prompt",
            "instructions": "You can deploy and manage OpenClaw instances on a VPS via Docker. When the user asks you to deploy, scale, or manage their OpenClaw infrastructure, use your deployment capabilities.",
        },
    },
]


async def seed_skills(db: AsyncSession):
    """Seed the database with default skills if they don't exist."""
    for skill_data in DEFAULT_SKILLS:
        # Check if skill already exists
        result = await db.execute(select(Skill).where(Skill.name == skill_data["name"]))
        existing = result.scalar_one_or_none()
        
        if not existing:
            skill = Skill(**skill_data)
            db.add(skill)
    
    await db.commit()


async def get_all_skills(db: AsyncSession, tier: str = None) -> list[Skill]:
    """Get all active skills, optionally filtered by tier."""
    query = select(Skill).where(Skill.is_active == "true")
    
    if tier:
        query = query.where(Skill.tier == tier)
    
    result = await db.execute(query)
    return result.scalars().all()


async def get_skill_by_name(db: AsyncSession, name: str) -> Skill:
    """Get a skill by its machine name."""
    result = await db.execute(select(Skill).where(Skill.name == name))
    return result.scalar_one_or_none()


async def validate_skill_selection(
    db: AsyncSession,
    skill_ids: list[str],
    user_api_keys: dict
) -> tuple[bool, str]:
    """
    Validate that selected skills can be used with provided API keys.
    Returns (is_valid, error_message)
    """
    for skill_id in skill_ids:
        result = await db.execute(select(Skill).where(Skill.id == skill_id))
        skill = result.scalar_one_or_none()
        
        if not skill:
            return False, f"Skill {skill_id} not found"
        
        # Check connected skills have required API keys
        if skill.tier == "connected":
            for env_var in skill.required_env_vars:
                # Map env_var to provider
                provider = env_var.lower().replace("_token", "").replace("_api_key", "")
                if provider not in user_api_keys or not user_api_keys[provider]:
                    return False, f"Skill '{skill.display_name}' requires {env_var}"
    
    return True, ""
