from __future__ import annotations

from pathlib import Path


def get_prompt_registry() -> list[dict]:
    prompts_dir = Path(__file__).parent.parent / "llm" / "prompts"
    registry = []
    for path in sorted(prompts_dir.glob("*.md")):
        version = "1.0"
        job_type = path.stem
        with open(path) as fh:
            content = fh.read()
        for line in content.splitlines()[:5]:
            if line.startswith("version:"):
                version = line.split(":", 1)[1].strip()
            if line.startswith("job_type:"):
                job_type = line.split(":", 1)[1].strip()
        registry.append({
            "name": path.stem,
            "job_type": job_type,
            "version": version,
            "path": str(path),
        })
    return registry
