import os
from typing import Dict, Any
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# .env 파일 활성화
load_dotenv()


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
    def get_llm(model_name: str, api_key: str, temperature: float = 0.7):
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
            default_headers={
                "HTTP-Referer": "https://github.com/crewAIInc/crewAI",
                "X-Title": "CrewAI TRPG"
            }
        )


# --- 편의 함수 ---

def get_builder_model(api_key=None):
    """
    빌더용: Meta Llama 3.3 70B Instruct (Free)
    """
    # [핵심] CrewAI(LiteLLM) 만족용으로 'openai/'를 붙여서 생성
    # 실제 전송은 OpenRouterLLM 클래스가 알아서 떼고 보냄
    return LLMFactory.get_llm("openai/tngtech/deepseek-r1t2-chimera:free", api_key, temperature=0.7)


def get_player_model(api_key=None):
    """
    플레이어/나레이터용: Meta Llama 3.3 70B Instruct (Free)
    """
    # 여기도 'openai/' 붙임
    return LLMFactory.get_llm("openai/tngtech/deepseek-r1t2-chimera:free", api_key, temperature=0.7)