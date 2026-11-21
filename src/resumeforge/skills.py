"""Skill name normalization. canonical('React.js') == canonical('reactjs') == 'React'."""
from __future__ import annotations

import re

# canonical name -> aliases (matched after lowercasing and removing spaces, dots, dashes, underscores)
ALIASES: dict[str, list[str]] = {
    "JavaScript": ["js", "javascript", "ecmascript", "es6"],
    "TypeScript": ["ts", "typescript"],
    "Python": ["py", "python", "python3"],
    "C++": ["cpp", "c++", "cplusplus"],
    "C#": ["csharp", "c#"],
    "Go": ["go", "golang"],
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3"],
    "Shell": ["shell", "bash", "shellscript", "sh", "zsh"],
    "Jupyter Notebook": ["jupyter", "jupyternotebook", "jupyternotebooks", "ipynb"],
    "SQL": ["sql"],
    "React": ["react", "reactjs", "react.js"],
    "React Native": ["reactnative"],
    "Next.js": ["next", "nextjs", "next.js"],
    "Vue": ["vue", "vuejs", "vue.js"],
    "Angular": ["angular", "angularjs"],
    "Svelte": ["svelte", "sveltekit"],
    "Tailwind CSS": ["tailwind", "tailwindcss"],
    "Vite": ["vite", "vitejs"],
    "Node.js": ["node", "nodejs", "node.js"],
    "Express": ["express", "expressjs", "express.js"],
    "NestJS": ["nest", "nestjs"],
    "FastAPI": ["fastapi"],
    "Flask": ["flask"],
    "Django": ["django"],
    "REST APIs": ["rest", "restapi", "restapis", "restful", "restfulapi", "restfulapis"],
    "GraphQL": ["graphql"],
    "WebSockets": ["websocket", "websockets", "socketio", "socket.io"],
    "PostgreSQL": ["postgres", "postgresql", "psql", "pg"],
    "MySQL": ["mysql"],
    "SQLite": ["sqlite", "sqlite3"],
    "MongoDB": ["mongo", "mongodb", "mongoose"],
    "Redis": ["redis"],
    "Supabase": ["supabase"],
    "Prisma": ["prisma", "prismaorm"],
    "Firebase": ["firebase"],
    "Docker": ["docker", "dockerfile", "dockercompose"],
    "Kubernetes": ["kubernetes", "k8s"],
    "AWS": ["aws", "amazonwebservices"],
    "Google Cloud": ["gcp", "googlecloud", "googlecloudplatform"],
    "Vercel": ["vercel"],
    "GitHub Actions": ["githubactions", "ghactions"],
    "CI/CD": ["cicd", "ci/cd", "continuousintegration"],
    "Git": ["git", "github", "versioncontrol"],
    "pandas": ["pandas"],
    "NumPy": ["numpy"],
    "scikit-learn": ["sklearn", "scikitlearn", "scikit-learn"],
    "PyTorch": ["pytorch", "torch"],
    "TensorFlow": ["tensorflow", "keras"],
    "Hugging Face": ["huggingface", "hf", "transformers", "huggingfacetransformers"],
    "LLM fine-tuning": ["finetuning", "llmfinetuning", "lora", "qlora", "peft", "finetuningllms"],
    "RAG": ["rag", "retrievalaugmentedgeneration"],
    "LLM APIs": ["llmapi", "llmapis", "llmintegration", "openaiapi", "anthropicapi", "claudeapi", "geminiapi"],
    "Prompt engineering": ["promptengineering", "prompting"],
    "AI agents": ["aiagents", "agents", "agenticai", "llmagents", "multiagentsystems"],
    "Machine learning": ["ml", "machinelearning"],
    "Deep learning": ["dl", "deeplearning"],
    "NLP": ["nlp", "naturallanguageprocessing"],
    "Computer vision": ["cv", "computervision", "opencv"],
    "Data visualization": ["dataviz", "datavisualization", "matplotlib", "seaborn", "plotly"],
    "OAuth": ["oauth", "oauth2", "googleoauth"],
    "JWT": ["jwt", "jsonwebtoken", "jsonwebtokens"],
    "Authentication": ["auth", "authentication", "userauthentication"],
    "Testing": ["testing", "unittesting", "automatedtesting"],
    "pytest": ["pytest"],
    "Playwright": ["playwright"],
}

_KEY = re.compile(r"[\s._\-]+")


def key(name: str) -> str:
    return _KEY.sub("", name.strip().lower())


_LOOKUP = {key(a): canon for canon, aliases in ALIASES.items() for a in [canon, *aliases]}


def canonical(name: str) -> str:
    """Canonical display name. Unknown names keep their own spelling with whitespace tidied."""
    clean = " ".join((name or "").split())
    return _LOOKUP.get(key(clean), clean)
