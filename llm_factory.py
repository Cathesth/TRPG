import os
from typing import Dict, Any, Generator
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# .env 파일 활성화
load_dotenv()

# 사용 가능한 모델 목록 (OpenRouter 형식)
AVAILABLE_MODELS = {
    # Google (2.0 → 2.5 → 3)
    "openai/google/gemini-2.0-flash-001": {"name": "Gemini 2.0 Flash", "provider": "Google", "context": "1M"},
    "openai/google/gemini-2.5-flash-lite": {"name": "Gemini 2.5 Flash Lite", "provider": "Google", "context": "1M"},
    "openai/google/gemini-2.5-flash": {"name": "Gemini 2.5 Flash", "provider": "Google", "context": "1M"},
    "openai/google/gemini-3-flash-preview": {"name": "Gemini 3 Flash (Preview)", "provider": "Google", "context": "1M"},
    "openai/google/gemini-3-pro-preview": {"name": "Gemini 3 Pro (Preview)", "provider": "Google", "context": "1M"},

    # Anthropic (3.5 → 4 → 4.5)
    "openai/anthropic/claude-3.5-haiku": {"name": "Claude 3.5 Haiku", "provider": "Anthropic", "context": "200K"},
    "openai/anthropic/claude-3.5-sonnet": {"name": "Claude 3.5 Sonnet", "provider": "Anthropic", "context": "200K"},
    "openai/anthropic/claude-sonnet-4": {"name": "Claude Sonnet 4", "provider": "Anthropic", "context": "200K"},
    "openai/anthropic/claude-haiku-4.5": {"name": "Claude Haiku 4.5", "provider": "Anthropic", "context": "200K"},
    "openai/anthropic/claude-sonnet-4.5": {"name": "Claude Sonnet 4.5", "provider": "Anthropic", "context": "200K"},
    "openai/anthropic/claude-opus-4.5": {"name": "Claude Opus 4.5", "provider": "Anthropic", "context": "200K"},

    # OpenAI (4o → 5)
    "openai/openai/gpt-4o-mini": {"name": "GPT-4o Mini", "provider": "OpenAI", "context": "128K"},
    "openai/openai/gpt-4o": {"name": "GPT-4o", "provider": "OpenAI", "context": "128K"},
    "openai/openai/gpt-5-mini": {"name": "GPT-5 Mini", "provider": "OpenAI", "context": "1M"},
    "openai/openai/gpt-5.2": {"name": "GPT-5.2", "provider": "OpenAI", "context": "1M"},

    # DeepSeek
    "openai/tngtech/deepseek-r1t2-chimera:free": {"name": "R1 Chimera (Free)", "provider": "DeepSeek", "context": "164K", "free": True},
    "openai/deepseek/deepseek-chat-v3-0324": {"name": "DeepSeek Chat V3", "provider": "DeepSeek", "context": "128K"},
    "openai/deepseek/deepseek-v3.2": {"name": "DeepSeek V3.2", "provider": "DeepSeek", "context": "128K"},

    # Meta Llama (3.1 → 3.3)
    "openai/meta-llama/llama-3.1-8b-instruct": {"name": "Llama 3.1 8B", "provider": "Meta", "context": "128K"},
    "openai/meta-llama/llama-3.1-405b-instruct:free": {"name": "Llama 3.1 405B (Free)", "provider": "Meta", "context": "128K", "free": True},
    "openai/meta-llama/llama-3.1-405b-instruct": {"name": "Llama 3.1 405B", "provider": "Meta", "context": "128K"},
    "openai/meta-llama/llama-3.3-70b-instruct:free": {"name": "Llama 3.3 70B (Free)", "provider": "Meta", "context": "128K", "free": True},
    "openai/meta-llama/llama-3.3-70b-instruct": {"name": "Llama 3.3 70B", "provider": "Meta", "context": "128K"},

    # xAI Grok (1 → 4 → 4.1)
    "openai/x-ai/grok-code-fast-1": {"name": "Grok Code Fast 1", "provider": "xAI", "context": "128K"},
    "openai/x-ai/grok-4-fast": {"name": "Grok 4 Fast", "provider": "xAI", "context": "256K"},
    "openai/x-ai/grok-4.1-fast": {"name": "Grok 4.1 Fast", "provider": "xAI", "context": "1M"},

    # Mistral AI
    "openai/mistralai/devstral-2512:free": {"name": "Devstral 2512 (Free)", "provider": "Mistral", "context": "128K", "free": True},

    # Xiaomi
    "openai/xiaomi/mimo-v2-flash:free": {"name": "MiMo V2 Flash (Free)", "provider": "Xiaomi", "context": "128K", "free": True},
}

# 기본 모델
DEFAULT_MODEL = "openai/tngtech/deepseek-r1t2-chimera:free"


class OpenRouterLLM(ChatOpenAI):
    """
    CrewAI와 OpenRouter 사이의 호환성 문제를 해결하기 위한 커스텀 래퍼.

    1. CrewAI(LiteLLM) 검사 통과용: 초기화할 때는 'openai/' 접두사가 붙은 모델명을 가짐.
    2. OpenRouter 전송용: 실제 API 호출 시(_default_params)에는 접두사를 떼고 보냄.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def _default_params(self) -> Dict[str, Any]:
        """LangChain이 API 요청 페이로드를 만들 때 호출하는 속성"""
        params = super()._default_params
        # 실제 전송 시 모델명에서 'openai/' 제거
        if "model" in params and str(params["model"]).startswith("openai/"):
            params["model"] = params["model"].replace("openai/", "")
        return params


class LLMFactory:
    @staticmethod
    def get_llm(model_name: str, api_key: str = None, temperature: float = 0.7, streaming: bool = False):
        # 모델 유효성 검사 - None 처리 추가
        if not model_name or model_name == 'None':
            model_name = DEFAULT_MODEL
        elif model_name not in AVAILABLE_MODELS:
            print(f"[경고] 알 수 없는 모델 '{model_name}', 기본 모델 '{DEFAULT_MODEL}' 사용")
            model_name = DEFAULT_MODEL

        # API Key 확인
        if not api_key:
            api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("API Key가 없습니다. .env 파일을 확인해주세요.")

        # [중요] CrewAI를 속이기 위한 환경변수 설정
        os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_API_BASE"] = "https://openrouter.ai/api/v1"

        return OpenRouterLLM(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model=model_name,  # 여기엔 'openai/'가 붙은 이름이 들어옴
            temperature=temperature,
            streaming=streaming,
            default_headers={
                "HTTP-Referer": "https://github.com/crewAIInc/crewAI",
                "X-Title": "CrewAI TRPG"
            }
        )

    @staticmethod
    def get_streaming_llm(model_name: str = None, api_key: str = None, temperature: float = 0.7):
        """스트리밍 지원 LLM 반환"""
        if model_name is None:
            model_name = DEFAULT_MODEL
        return LLMFactory.get_llm(model_name, api_key, temperature, streaming=True)


# --- 편의 함수 ---

def get_builder_model(model_name: str = None, api_key: str = None):
    """
    빌더용 모델 반환
    """
    if model_name is None:
        model_name = DEFAULT_MODEL
    return LLMFactory.get_llm(model_name, api_key, temperature=0.7)


def get_player_model(model_name: str = None, api_key: str = None):
    """
    플레이어/나레이터용 모델 반환
    """
    if model_name is None:
        model_name = DEFAULT_MODEL
    return LLMFactory.get_llm(model_name, api_key, temperature=0.7)


def get_streaming_model(model_name: str = None, api_key: str = None):
    """
    스트리밍용 모델 반환
    """
    return LLMFactory.get_streaming_llm(model_name=model_name, api_key=api_key)
