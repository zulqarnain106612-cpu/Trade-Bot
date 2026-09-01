import os

from dotenv import load_dotenv

load_dotenv()

COORDINATOR_MODEL = os.getenv("COORDINATOR_MODEL", "sonnet")
WORKER_MODEL = os.getenv("WORKER_MODEL", "haiku")
MAX_SUBTASKS = int(os.getenv("MAX_SUBTASKS", "5"))
MAX_WORKER_TURNS = int(os.getenv("MAX_WORKER_TURNS", "6"))
WORKER_ALLOWED_TOOLS = os.getenv("WORKER_ALLOWED_TOOLS", "Read,Bash,Grep,Glob")
