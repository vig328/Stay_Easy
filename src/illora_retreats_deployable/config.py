import os
from dotenv import load_dotenv

load_dotenv()

import os

class Config:
    # Google Sheets
    GSHEET_ID = "1Wv7lOg8yjsK12hve4CauZFTL_hVJrwY3j7MObW_2q1E"
    SERVICE_ACCOUNT_FILE = r"E:\ilora_case_study-main\src\illora_retreats_deployable\service_account.json"
    
    
    GSHEET_QNA_SHEET = "QnA_Manager"
    GSHEET_DOS_SHEET = "Dos and Donts"
    GSHEET_CAMPAIGN_SHEET = "Campaigns_Manager"

    # ------------------------
    # LLM Provider (switch between "openai" and "groq")
    # ------------------------
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()

    # ------------------------
    # Anthropic Claude (LLM)
    # ------------------------
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-opus-20240229")

    # ------------------------
    # Groq (fallback)
    # ------------------------
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    GROQ_API_BASE = os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1")

    # ------------------------
    # Stripe Payments
    # ------------------------
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

    # ------------------------
    # Data paths
    # ------------------------
    CSV_DATA_PATH = os.getenv("CSV_DATA_PATH", "data\\qa_pairs.csv")
    RAW_DOCS_DIR = "data\\raw_docs"
    SUMMARY_OUTPUT_PATH = "data\\combined_summary.txt"
    QA_OUTPUT_CSV = "data\\qa_pairs.csv"
    UPLOAD_TEMP_DIR = "Hotel_docs"

    # ------------------------
    # QNA generation
    # ------------------------
    MAX_SUMMARY_TOKENS = int(os.getenv("MAX_SUMMARY_TOKENS", "500"))
    QA_PAIR_COUNT = int(os.getenv("QA_PAIR_COUNT", "100"))

    # ------------------------
    # Github Token for AI use
    # ------------------------
    endpoint = "https://models.github.ai/inference"
    model = "openai/gpt-5"
    GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]







