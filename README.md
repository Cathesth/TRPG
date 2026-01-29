# 🌊 YEOUL: Interactive AI TRPG Platform

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![LangChain](https://img.shields.io/badge/LangGraph-Stateful_Agent-green?style=for-the-badge)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16.0-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)
![TailwindCSS](https://img.shields.io/badge/TailwindCSS-Unique_Design-06B6D4?style=for-the-badge&logo=tailwindcss&logoColor=white)

> **"상상하는 모든 모험이 현실이 되는 곳"**  
> **YEOUL(여울)**은 누구나 쉽게 시나리오를 창작하고, AI 게임마스터와 함께 1:1 모험을 즐길 수 있는 차세대 텍스트 RPG 플랫폼입니다.

---

## ✨ Key Features

### 1. 🎭 AI Game Master (Player Mode)
사용자의 선택에 따라 무한한 이야기가 펼쳐집니다. 정해진 선택지뿐만 아니라 **자유로운 자연어 행동**도 완벽하게 처리합니다.
- **LangGraph 기반 State 관리**: 복잡한 게임 상태를 체계적으로 관리하여 맥락을 놓치지 않습니다.
- **실시간 스트리밍 (SSE)**: 글자가 한 자 한 자 써지는 듯한 몰입감 있는 경험을 제공합니다.
- **동적 이미지 생성**: 상황에 맞는 배경과 NPC 이미지를 AI가 실시간으로 그려냅니다.

### 2. 🛠️ Scenario Builder (Creator Mode)
코딩 없이 오직 **상상력**만으로 나만의 세계를 만드세요.
- **노드 기반 비주얼 에디터**: 씬(Scene)과 선택지(Choice)를 드래그 앤 드롭으로 연결하세요.
- **AI 보조 도구 (Copilot)**:
  - **Magic Write**: "음산한 폐가"라고만 적으면 AI가 멋진 묘사를 써줍니다.
  - **NPC Generator**: 성격만 입력하면 알맞은 말투와 배경 설정을 만들어줍니다.
  - **AI Audit**: 시나리오의 논리적 오류나 끊긴 부분을 AI가 검수해 줍니다.
- **Draft & History**: 작업 내용은 자동 저장되며, 언제든 이전 시점으로 되돌릴 수 있습니다(Undo/Redo).

### 3. 🤖 AI Guide Assistant
서비스 이용 중 궁금한 점은 AI 가이드 **'여울'**에게 물어보세요.
- **RAG (Retrieval-Augmented Generation)**: 매뉴얼 문서를 검색해 정확한 사용법을 알려줍니다.
- **Hybrid Response**: 자주 묻는 질문은 키워드 매칭으로 즉답하고, 복잡한 질문은 LLM이 친절하게 설명합니다.

---

## 🏗️ Architecture

YEOUL은 **멀티 에이전트(Multi-Agent)** 시스템을 기반으로 설계되었습니다. 각 에이전트는 독립적인 역할(Role)을 수행하며 협력합니다.

```mermaid
flowchart TD
    User((User)) -->|Action| API_Gateway
    
    subgraph "Core Agents"
        API_Gateway -->|Play| GameEngine["🎮 Game Agent<br/>(Rules, Story, Combat)"]
        API_Gateway -->|Create| BuilderAgent["🛠️ Builder Agent<br/>(Gen, Audit, Refine)"]
        API_Gateway -->|Help| GuideAgent["💁‍♀️ Guide Agent<br/>(RAG + LLM)"]
    end
    
    subgraph "Infrastructure"
        GameEngine & BuilderAgent --> LLM{{"LLM Core"}}
        GameEngine <--> DB[("PostgreSQL")]
        GameEngine <--> Redis[("Redis Cache")]
        GuideAgent <--> VecDB[("Vector DB")]
    end
```

### Tech Stack
- **Backend**: Python, FastAPI, SQLAlchemy
- **AI/LLM**: LangChain, LangGraph, OpenAI GPT-4o / DeepSeek
- **Frontend**: HTML5, Vanilla JS (Module), TailwindCSS
- **Database**: PostgreSQL (Railway), Redis (Upstash)
- **Deployment**: Railway Cloud

---

## 🚀 Getting Started

### Prerequisites
- Python 3.11+
- OpenAI API Key (or DeepSeek API Key)
- PostgreSQL Database

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/Cathesth/semi-prj-3.git
   cd semi-prj-3
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Environment Setup**
   `.env` 파일을 생성하고 아래 내용을 입력하세요.
   ```env
   DATABASE_URL=postgresql://user:pass@localhost:5432/yeoul_db
   OPENAI_API_KEY=sk-...
   REDIS_URL=redis://...
   ```

4. **Run Server**
   ```bash
   uvicorn app:app --reload
   ```
   Now visit `http://localhost:8000` 🚀

---

## 📸 Screenshots

| Main Lobby | In-Game Play | Builder Canvas |
|:---:|:---:|:---:|
| <img src="docs/previews/main.png" width="250"> | <img src="docs/previews/play.png" width="250"> | <img src="docs/previews/builder.png" width="250"> |

---

## 🤝 Contribution

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

> **Note**: This project was developed as a semi-project implementing advanced AI agent workflows using LangGraph.
